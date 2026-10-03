"use client";

import { useEffect, useMemo, useState, type RefObject } from "react";
import type { FormaProjectSummary } from "../contracts";
import { FormaApiClient, FormaApiError } from "../api";

export type FormaProjectBrowserProps = {
  client?: FormaApiClient;
  /** Pass this prop to use controlled data from an existing application query. */
  projects?: FormaProjectSummary[];
  totalItems?: number;
  currentPage?: number;
  onPageChange?: (page: number) => void;
  searchValue?: string;
  onSearchValueChange?: (value: string) => void;
  scope?: "community" | "mine";
  pageSize?: number;
  title?: string;
  loading?: boolean;
  error?: unknown;
  onOpenProject?: (projectId: string, project: FormaProjectSummary) => void;
  onToggleSave?: (project: FormaProjectSummary) => void | Promise<void>;
  onRemixProject?: (project: FormaProjectSummary) => void | Promise<void>;
  onVisibleProjectIdsChange?: (projectIds: string[]) => void;
  sectionRef?: RefObject<HTMLElement | null>;
  className?: string;
};

type LocalState = {
  projects: FormaProjectSummary[];
  total: number;
  loading: boolean;
  error: unknown;
};

function errorLabel(error: unknown) {
  if (error instanceof FormaApiError && error.unauthorized) return "Authorization required";
  if (error instanceof Error && error.message) return error.message;
  return "Projects could not be loaded. Check the Forma API and try again.";
}

function imageSrc(project: FormaProjectSummary) {
  const direct = project.image_url || project.product_image_url;
  if (typeof direct === "string" && direct.trim()) return direct;
  if (typeof project.product_image_data === "string" && project.product_image_data.trim()) {
    return project.product_image_data.startsWith("data:")
      ? project.product_image_data
      : `data:${project.product_image_content_type || "image/png"};base64,${project.product_image_data}`;
  }
  return null;
}

function formatAge(value: string | null | undefined) {
  if (!value) return "";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "";
  const days = Math.floor(Math.max(0, Date.now() - timestamp) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "1 day";
  if (days < 31) return `${days} days`;
  const months = Math.floor(days / 30);
  return months === 1 ? "1 month" : `${months} months`;
}

function displayName(project: FormaProjectSummary) {
  return project.creator_display?.trim() || project.creator_username?.trim() || "Forma builder";
}

export function FormaProjectBrowser({
  client,
  projects,
  totalItems,
  currentPage,
  onPageChange,
  searchValue,
  onSearchValueChange,
  scope = "community",
  pageSize = 12,
  title = "Projects",
  loading: controlledLoading,
  error: controlledError,
  onOpenProject,
  onToggleSave,
  onRemixProject,
  onVisibleProjectIdsChange,
  sectionRef,
  className = "",
}: FormaProjectBrowserProps) {
  const controlled = projects !== undefined;
  const [localSearch, setLocalSearch] = useState("");
  const [localPage, setLocalPage] = useState(0);
  const [state, setState] = useState<LocalState>({ projects: [], total: 0, loading: !controlled && Boolean(client), error: null });
  const search = searchValue ?? localSearch;
  const page = currentPage ?? localPage;

  useEffect(() => {
    if (controlled || !client) return;
    let active = true;
    setState((current) => ({ ...current, loading: true, error: null }));
    void client.listProjects({ scope, search, limit: pageSize, offset: page * pageSize })
      .then((result) => {
        if (active) setState({ projects: result.items, total: result.total, loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (active) setState({ projects: [], total: 0, loading: false, error });
      });
    return () => {
      active = false;
    };
  }, [client, controlled, page, pageSize, scope, search]);

  const visibleProjects = controlled ? projects : state.projects;
  const total = totalItems ?? (controlled ? visibleProjects.length : state.total);
  const loading = controlledLoading ?? (!controlled && state.loading);
  const error = controlledError ?? (!controlled && !client ? new Error("Configure a FormaApiClient to load projects.") : state.error);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(Math.max(page, 0), pageCount - 1);
  const visibleIds = useMemo(() => visibleProjects.map((project) => project.project_id), [visibleProjects]);
  const setPage = (nextPage: number) => {
    const next = Math.min(Math.max(nextPage, 0), pageCount - 1);
    if (onPageChange) onPageChange(next);
    else setLocalPage(next);
  };
  const setSearch = (value: string) => {
    if (onSearchValueChange) onSearchValueChange(value);
    else {
      setLocalSearch(value);
      setLocalPage(0);
    }
  };

  useEffect(() => {
    onVisibleProjectIdsChange?.(visibleIds);
  }, [onVisibleProjectIdsChange, visibleIds]);

  return (
    <section ref={sectionRef} className={`forma-gui forma-gui-browser ${className}`} aria-busy={loading}>
      <div className="forma-gui-browser-heading">
        <div>
          <h2>{title}</h2>
          <p>{loading ? "Loading projects..." : `${total} ${total === 1 ? "project" : "projects"}`}</p>
        </div>
        <label className="forma-gui-search">
          <span className="forma-gui-visually-hidden">Search projects</span>
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search projects" />
        </label>
      </div>

      {loading ? (
        <div className="forma-gui-card-grid" aria-label="Loading projects">
          {Array.from({ length: Math.min(pageSize, 8) }, (_, index) => <div className="forma-gui-card forma-gui-card-skeleton" key={index} />)}
        </div>
      ) : error ? (
        <div className="forma-gui-state forma-gui-state-error" role="alert">
          <strong>{error instanceof FormaApiError && error.unauthorized ? "Sign-in required" : "Projects unavailable"}</strong>
          <span>{errorLabel(error)}</span>
        </div>
      ) : visibleProjects.length === 0 ? (
        <div className="forma-gui-state"><strong>{search.trim() ? "No matching projects" : "No projects yet"}</strong><span>{search.trim() ? "Try a different search." : "Generated projects will appear here."}</span></div>
      ) : (
        <>
          <div className="forma-gui-card-grid">
            {visibleProjects.map((project) => <ProjectCard key={project.project_id} project={project} onOpenProject={onOpenProject} onToggleSave={onToggleSave} onRemixProject={onRemixProject} />)}
          </div>
          {pageCount > 1 && (
            <nav className="forma-gui-pagination" aria-label="Project pages">
              <button type="button" onClick={() => setPage(safePage - 1)} disabled={safePage === 0}>Previous</button>
              <span>Page {safePage + 1} of {pageCount}</span>
              <button type="button" onClick={() => setPage(safePage + 1)} disabled={safePage >= pageCount - 1}>Next</button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

function ProjectCard({
  project,
  onOpenProject,
  onToggleSave,
  onRemixProject,
}: {
  project: FormaProjectSummary;
  onOpenProject?: FormaProjectBrowserProps["onOpenProject"];
  onToggleSave?: FormaProjectBrowserProps["onToggleSave"];
  onRemixProject?: FormaProjectBrowserProps["onRemixProject"];
}) {
  const [saveBusy, setSaveBusy] = useState(false);
  const [remixBusy, setRemixBusy] = useState(false);
  const image = imageSrc(project);
  const open = () => onOpenProject?.(project.project_id, project);
  const save = async (event: React.MouseEvent) => {
    event.stopPropagation();
    if (!onToggleSave || saveBusy) return;
    setSaveBusy(true);
    try { await onToggleSave(project); } finally { setSaveBusy(false); }
  };
  const remix = async (event: React.MouseEvent) => {
    event.stopPropagation();
    if (!onRemixProject || remixBusy) return;
    setRemixBusy(true);
    try { await onRemixProject(project); } finally { setRemixBusy(false); }
  };

  return (
    <article className="forma-gui-card" role={onOpenProject ? "link" : undefined} tabIndex={onOpenProject ? 0 : undefined} onClick={open} onKeyDown={(event) => { if (onOpenProject && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } }}>
      <div className="forma-gui-card-image">
        {image ? <img src={image} alt={`${project.title} preview`} /> : <span aria-hidden="true">FORMA / PROJECT</span>}
      </div>
      <div className="forma-gui-card-body">
        <div className="forma-gui-card-title-row"><h3>{project.title || "Untitled project"}</h3><span>{project.parts_count || 0} parts</span></div>
        <p className="forma-gui-card-description">{project.description || project.prompt || "Hardware project"}</p>
        <div className="forma-gui-card-meta"><span>{displayName(project)}</span><span>{formatAge(project.created_at)}</span></div>
        {(onToggleSave || onRemixProject) && <div className="forma-gui-card-actions">
          {onToggleSave && <button type="button" onClick={save} disabled={saveBusy} aria-pressed={Boolean(project.saved)}>{project.saved ? "Saved" : "Save"}</button>}
          {onRemixProject && <button type="button" onClick={remix} disabled={remixBusy}>Remix</button>}
        </div>}
      </div>
    </article>
  );
}
