"use client";

import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { History, LoaderCircle, RefreshCw, X } from "lucide-react";
import {
  mergeRevisionPages, parseRevisionPage, parseRevisionSnapshot, readHistoryResponse, revisionId,
  type ProjectRevisionPage, type ProjectRevisionSnapshot,
} from "../../lib/project-history";
import styles from "./chat-project-layout.module.css";

export type ProjectHistoryConfig = {
  projectId: string;
  identityKey: string;
  apiUrl: string;
  enabled: boolean;
  latestRevision: number | null;
  getHeaders: () => Promise<Record<string, string>>;
  loadLatest: (signal: AbortSignal) => Promise<boolean>;
};

type Selection = { id: string; snapshot: ProjectRevisionSnapshot | null; error: string | null };
type HistoryState = {
  config: ProjectHistoryConfig;
  drawerId: string;
  open: boolean;
  setOpen: (value: boolean) => void;
  page: ProjectRevisionPage | null;
  listLoading: boolean;
  listError: string | null;
  loadPage: (before?: number) => void;
  selection: Selection | null;
  select: (id: string) => void;
  latestRevision: number | null;
  returning: boolean;
  returnError: string | null;
  returnToLatest: () => void;
};
const ProjectHistoryContext = createContext<HistoryState | null>(null);
export const useProjectHistory = () => useContext(ProjectHistoryContext);

export function ProjectHistoryProvider({ config, children }: { config?: ProjectHistoryConfig; children: ReactNode }) {
  return config?.enabled && config.projectId
    ? <ProjectHistorySession key={`${config.identityKey}:${config.projectId}`} config={config}>{children}</ProjectHistorySession>
    : <ProjectHistoryContext.Provider value={null}>{children}</ProjectHistoryContext.Provider>;
}

function updateRevisionUrl(id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("revision", id);
  else url.searchParams.delete("revision");
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}

function ProjectHistorySession({ config, children }: { config: ProjectHistoryConfig; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<ProjectRevisionPage | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [returning, setReturning] = useState(false);
  const [returnError, setReturnError] = useState<string | null>(null);
  const listRequest = useRef<AbortController | null>(null);
  const snapshotRequest = useRef<AbortController | null>(null);
  const latestRequest = useRef<AbortController | null>(null);
  const configRef = useRef(config);
  configRef.current = config;
  const drawerId = useId();
  const latestRevision = Math.max(config.latestRevision || 0, page?.latest_revision || 0) || null;

  const loadPage = useCallback(async (before?: number) => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setListLoading(true);
    setListError(null);
    const current = configRef.current;
    try {
      const query = before ? `?before=${before}` : "";
      const response = await fetch(`${current.apiUrl}/projects/${encodeURIComponent(current.projectId)}/history${query}`, {
        headers: await current.getHeaders(), signal: controller.signal, cache: "no-store",
      });
      const next = parseRevisionPage(await readHistoryResponse(response), current.projectId);
      if (controller.signal.aborted) return;
      setPage((previous) => ({ ...next, items: before ? mergeRevisionPages(previous?.items || [], next.items) : next.items }));
    } catch (error) {
      if (!controller.signal.aborted) setListError(error instanceof Error ? error.message : "Version history could not be loaded.");
    } finally {
      if (!controller.signal.aborted) setListLoading(false);
    }
  }, []);

  const select = useCallback(async (id: string) => {
    snapshotRequest.current?.abort();
    latestRequest.current?.abort();
    const controller = new AbortController();
    snapshotRequest.current = controller;
    setReturning(false);
    setReturnError(null);
    setSelection({ id, snapshot: null, error: null });
    updateRevisionUrl(id);
    const current = configRef.current;
    try {
      if (!revisionId(id)) throw new Error("This saved version link is invalid.");
      const response = await fetch(`${current.apiUrl}/projects/${encodeURIComponent(current.projectId)}/history/${encodeURIComponent(id)}`, {
        headers: await current.getHeaders(), signal: controller.signal, cache: "no-store",
      });
      const snapshot = parseRevisionSnapshot(await readHistoryResponse(response), current.projectId, id);
      if (!controller.signal.aborted) setSelection({ id, snapshot, error: null });
    } catch (error) {
      if (!controller.signal.aborted) setSelection({ id, snapshot: null, error: error instanceof Error ? error.message : "This saved version could not be loaded." });
    }
  }, []);

  const returnToLatest = useCallback(async () => {
    latestRequest.current?.abort();
    const controller = new AbortController();
    latestRequest.current = controller;
    setReturning(true);
    setReturnError(null);
    try {
      if (!(await configRef.current.loadLatest(controller.signal))) throw new Error("The latest project could not be loaded. Please try again.");
      if (controller.signal.aborted) return;
      snapshotRequest.current?.abort();
      setSelection(null);
      updateRevisionUrl(null);
      void loadPage();
    } catch (error) {
      if (!controller.signal.aborted) setReturnError(error instanceof Error ? error.message : "The latest project could not be loaded.");
    } finally {
      if (!controller.signal.aborted) setReturning(false);
    }
  }, [loadPage]);

  useEffect(() => {
    if (open) void loadPage();
  }, [open, config.latestRevision, loadPage]);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("revision");
    if (id) { setOpen(true); void select(id); }
    return () => {
      listRequest.current?.abort(); snapshotRequest.current?.abort(); latestRequest.current?.abort();
    };
  }, [select]);

  return <ProjectHistoryContext.Provider value={{
    config, drawerId, open, setOpen, page, listLoading, listError, loadPage, selection, select,
    latestRevision, returning, returnError, returnToLatest,
  }}>{children}</ProjectHistoryContext.Provider>;
}

export function ProjectHistoryButton() {
  const history = useProjectHistory();
  if (!history) return null;
  return <button type="button" className={styles.button} aria-expanded={history.open} aria-controls={history.drawerId}
    onClick={() => history.setOpen(!history.open)}>
    <History className={styles.icon} /> History
  </button>;
}

export function ProjectVersionLabel() {
  const history = useProjectHistory();
  if (!history) return <>Current project</>;
  return <>{history.selection
    ? history.selection.snapshot ? `Viewing v${history.selection.snapshot.revision}` : "Saved version"
    : history.latestRevision ? `v${history.latestRevision} · Latest` : "Latest"}</>;
}

export function ProjectHistoryBody({ children }: { children: ReactNode }) {
  const history = useProjectHistory();
  const panelRef = useRef<HTMLElement>(null);
  function close() {
    if (!history) return;
    history.setOpen(false);
    document.querySelector<HTMLButtonElement>(`button[aria-controls="${CSS.escape(history.drawerId)}"]`)?.focus();
  }
  return <>
    {history?.selection && <div className={styles.versionNotice} role="status">
      <span>{history.selection.snapshot ? `Viewing saved version ${history.selection.snapshot.revision}.` : "Opening a saved version."} Chat changes apply to the latest project.</span>
      <button type="button" className={styles.button} onClick={history.returnToLatest} disabled={history.returning}>
        {history.returning ? "Loading latest…" : `Return to latest${history.latestRevision ? `: v${history.latestRevision}` : ""}`}
      </button>
    </div>}
    {history?.returnError && <p className={styles.historyError} role="alert">{history.returnError}</p>}
    <div className={styles.historyLayout}>
      <div className={styles.surfaceContent}>{children}</div>
      {history?.open && <aside id={history.drawerId} ref={panelRef} className={styles.historyPanel} aria-label="Version history"
        onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
        <header className={styles.historyHeader}>
          <h2>Version history</h2>
          <button type="button" className={styles.button} onClick={() => history.loadPage()} disabled={history.listLoading} aria-label="Refresh version history"><RefreshCw className={styles.icon} /></button>
          <button type="button" className={styles.button} onClick={close} aria-label="Close version history"><X className={styles.icon} /></button>
        </header>
        <div className={styles.historyEntries}>
          {history.listError && <div className={styles.historyError} role="alert">{history.listError}
            <button type="button" className={styles.button} onClick={() => history.loadPage()}>Try again</button>
          </div>}
          {history.listLoading && <p className={styles.historyEmpty} role="status"><LoaderCircle className={styles.icon} /> Loading versions…</p>}
          {!history.listLoading && !history.listError && history.page?.items.length === 0 && <p className={styles.historyEmpty}>No saved versions yet. Earlier projects may only have their current state.</p>}
          <ol className={styles.historyList}>{history.page?.items.map((item) => {
            const viewing = history.selection?.id === item.revision_id;
            const latest = item.revision === history.latestRevision;
            return <li key={item.revision_id}><button type="button" className={styles.historyEntry} aria-current={viewing ? "true" : undefined}
              onClick={() => {
                history.select(item.revision_id);
                if ((panelRef.current?.parentElement?.clientWidth || 0) <= 640) close();
              }}>
              <span className={styles.historyEntryTitle}><strong>v{item.revision}</strong>{latest && <span>Latest</span>}{viewing && <span>Viewing</span>}</span>
              <span>{item.summary}</span>
              <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</time>
            </button></li>;
          })}</ol>
          {history.page?.next_before && <button type="button" className={styles.button} disabled={history.listLoading}
            onClick={() => history.loadPage(history.page!.next_before!)}>Load older versions</button>}
        </div>
      </aside>}
    </div>
  </>;
}
