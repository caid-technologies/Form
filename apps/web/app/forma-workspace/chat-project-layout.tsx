"use client";

import {
  createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState,
  type CSSProperties, type ReactNode,
} from "react";
import { ArrowUpRight, Layers, Maximize2, MessageSquare, Minimize2, X } from "lucide-react";
import {
  CHAT_PROJECT_SPLIT_MIN_WIDTH, MAX_CHAT_FRACTION, MIN_CHAT_FRACTION,
  clampChatFraction, completedProjectReference, initialChatProjectLayout,
  linkedProjectPath, projectPaneVisible, type ProjectMessageReference,
} from "../../lib/chat-project-layout";
import styles from "./chat-project-layout.module.css";
import { revisionId } from "../../lib/project-history";
import { ShareProjectButton } from "./project-share-button";
import {
  ProjectHistoryProvider, ProjectHistoryButton, ProjectHistoryBody, ProjectVersionLabel, useProjectHistory,
  type ProjectHistoryConfig,
} from "./project-history";

type ProjectWorkspaceContext = {
  projectId: string | null;
  hasProject: boolean;
  fullScreen: boolean;
  openProject: () => void;
  closeProject: () => void;
  toggleFullScreen: () => void;
};
const ProjectWorkspace = createContext<ProjectWorkspaceContext | null>(null);

type ChatProjectLayoutProps = {
  conversationKey: string;
  projectId?: string | null;
  project?: ReactNode;
  children: ReactNode;
  history?: ProjectHistoryConfig;
};

/** One session owns one viewer slot. Changing chats releases the previous subtree. */
export default function ChatProjectLayout(props: ChatProjectLayoutProps) {
  return <ProjectHistoryProvider key={props.conversationKey} config={props.history?.projectId === props.projectId ? props.history : undefined}>
    <ChatProjectLayoutSession key={props.conversationKey} {...props} />
  </ProjectHistoryProvider>;
}

function ChatProjectLayoutSession({ conversationKey, projectId = null, project, children }: ChatProjectLayoutProps) {
  const [state, setState] = useState(initialChatProjectLayout);
  const [wide, setWide] = useState(false);
  const frameRef = useRef<HTMLDivElement>(null);
  const projectRef = useRef<HTMLElement>(null);
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const projectDomId = useId();
  const chatDomId = useId();
  const hasProject = Boolean(project);
  const selectedRevision = useProjectHistory()?.selection?.id;
  const visible = projectPaneVisible(state, hasProject, wide);
  const fullScreen = visible && state.fullScreen;
  const chatHidden = visible && (!wide || fullScreen);
  const split = visible && wide && !fullScreen;

  useEffect(() => {
    const element = frameRef.current;
    if (!element) return;
    const measure = () => setWide(element.getBoundingClientRect().width >= CHAT_PROJECT_SPLIT_MIN_WIDTH);
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const openProject = useCallback(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setState((current) => ({ ...current, desktopOpen: true, narrowPane: "project" }));
  }, []);

  useEffect(() => {
    if (selectedRevision) openProject();
  }, [selectedRevision, openProject]);

  const closeProject = useCallback(() => {
    setState((current) => ({ ...current, desktopOpen: false, narrowPane: "chat", fullScreen: false }));
    // A close button is removed with the viewer; restore focus to a surviving control.
    requestAnimationFrame(() => {
      const opener = openerRef.current;
      (opener?.isConnected && opener.getClientRects().length ? opener : openButtonRef.current)?.focus();
    });
  }, []);

  const toggleFullScreen = useCallback(() => {
    setState((current) => ({ ...current, desktopOpen: true, narrowPane: "project", fullScreen: !current.fullScreen }));
  }, []);

  useEffect(() => {
    if (chatHidden && !fullScreen) projectRef.current?.focus();
  }, [chatHidden, fullScreen]);

  useEffect(() => {
    if (!fullScreen || !projectRef.current) return;
    const pane = projectRef.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const restoreBackground = makeBackgroundInert(pane);
    document.body.style.overflow = "hidden";
    pane.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setState((current) => ({ ...current, fullScreen: false }));
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(pane.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.getClientRects().length > 0 && !element.closest("[inert]"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) { event.preventDefault(); pane.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || !focusable.includes(document.activeElement as HTMLElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !focusable.includes(document.activeElement as HTMLElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreBackground();
      if (previousFocus?.isConnected && previousFocus.getClientRects().length) previousFocus.focus();
    };
  }, [fullScreen]);

  const context = useMemo<ProjectWorkspaceContext>(() => ({
    projectId, hasProject, fullScreen, openProject, closeProject, toggleFullScreen,
  }), [projectId, hasProject, fullScreen, openProject, closeProject, toggleFullScreen]);
  const gridStyle: CSSProperties = {
    gridTemplateColumns: split
      ? `minmax(0, ${state.chatFraction}fr) 10px minmax(0, ${1 - state.chatFraction}fr)`
      : "minmax(0, 1fr)",
  };

  return (
    <ProjectWorkspace.Provider value={context}>
      <div ref={frameRef} className={styles.frame} data-testid="chat-project-layout" data-layout={split ? "split" : visible ? "project" : "chat"}>
        {hasProject && (
          <div className={styles.toolbar} role="group" aria-label="Workspace view" hidden={fullScreen}>
            <button type="button" className={styles.button} onClick={closeProject} aria-pressed={!visible} aria-controls={chatDomId}>
              <MessageSquare className={styles.icon} /> {wide ? "Chat only" : "Chat"}
            </button>
            <button ref={openButtonRef} type="button" className={styles.button} onClick={openProject} aria-pressed={visible} aria-controls={projectDomId} aria-label="Show project">
              <Layers className={styles.icon} /> {wide ? "Chat + Project" : "Project"}
            </button>
            <span className={styles.hint}>{split ? "Drag the divider to resize" : "One workspace, linked from the conversation"}</span>
          </div>
        )}
        <div className={styles.grid} style={gridStyle}>
          <div id={chatDomId} className={styles.chat} hidden={chatHidden} data-testid="chat-pane">{children}</div>
          {split && (
            <div
              className={styles.separator} role="separator" tabIndex={0} aria-label="Resize chat and project"
              aria-orientation="vertical" aria-controls={chatDomId}
              aria-valuemin={MIN_CHAT_FRACTION * 100} aria-valuemax={MAX_CHAT_FRACTION * 100}
              aria-valuenow={Math.round(state.chatFraction * 100)}
              onPointerDown={(event) => { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); }}
              onPointerMove={(event) => {
                if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
                const bounds = frameRef.current?.getBoundingClientRect();
                if (!bounds?.width) return;
                const fraction = clampChatFraction((event.clientX - bounds.left) / bounds.width);
                setState((current) => ({ ...current, chatFraction: fraction }));
              }}
              onPointerUp={(event) => {
                if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
              }}
              onKeyDown={(event) => {
                const value = event.key === "ArrowLeft" ? state.chatFraction - 0.02
                  : event.key === "ArrowRight" ? state.chatFraction + 0.02
                    : event.key === "Home" ? MIN_CHAT_FRACTION : event.key === "End" ? MAX_CHAT_FRACTION : null;
                if (value === null) return;
                event.preventDefault();
                setState((current) => ({ ...current, chatFraction: clampChatFraction(value) }));
              }}
            />
          )}
          {visible && (
            <section
              id={projectDomId} ref={projectRef} tabIndex={-1}
              className={`${styles.project} ${fullScreen ? styles.fullScreen : ""}`}
              role={fullScreen ? "dialog" : "region"} aria-modal={fullScreen || undefined}
              aria-label="Project workspace" data-testid="project-pane"
            >
              <div className={styles.surface} key={projectId || conversationKey}>{project}</div>
            </section>
          )}
        </div>
      </div>
    </ProjectWorkspace.Provider>
  );
}

/** The same surface stays mounted when expanded; closing it unmounts the viewer. */
export function ChatProjectSurface({ title, children, leading, projectId, shareTitle, isPrivate = false }: {
  title: ReactNode;
  children: ReactNode;
  leading?: ReactNode;
  projectId?: string | null;
  shareTitle?: string;
  isPrivate?: boolean;
}) {
  const workspace = useContext(ProjectWorkspace);
  return (
    <div className={styles.surface} data-testid="project-surface">
      <header className={`${styles.surfaceHeader} ${leading ? styles.standaloneHeader : ""}`}>
        {leading}
        <div className={styles.identity}>
          <div className={styles.eyebrow}><Layers className={styles.icon} /> <ProjectVersionLabel /></div>
          <div className={styles.surfaceTitle}>{title}</div>
        </div>
        <div className={styles.actions} role="group" aria-label="Project surface">
          {projectId && <ShareProjectButton projectId={projectId} title={shareTitle || "Forma project"} isPrivate={isPrivate} />}
          <ProjectHistoryButton />
          {workspace && (
            <>
              <button type="button" className={styles.button} onClick={workspace.toggleFullScreen} aria-pressed={workspace.fullScreen} aria-label={workspace.fullScreen ? "Exit project full screen" : "View project full screen"}>
                {workspace.fullScreen ? <Minimize2 className={styles.icon} /> : <Maximize2 className={styles.icon} />}
                <span className={styles.buttonLabel}>{workspace.fullScreen ? "Exit full screen" : "Full screen"}</span>
              </button>
              <button type="button" className={styles.button} onClick={workspace.closeProject} aria-label="Close project"><X className={styles.icon} /></button>
            </>
          )}
        </div>
      </header>
      <ProjectHistoryBody>{children}</ProjectHistoryBody>
    </div>
  );
}

/** References only: never mount project/CAD content inside a message. */
export function ProjectUpdateCard({ message }: { message: ProjectMessageReference }) {
  const workspace = useContext(ProjectWorkspace);
  const history = useProjectHistory();
  const id = completedProjectReference(message);
  if (!id) return null;
  const current = Boolean(workspace?.hasProject && workspace.projectId === id);
  const savedRevision = revisionId(message.revisionId);
  const content = <>
    <Layers className={styles.cardIcon} />
    <span className={styles.cardText}>
      <span className={styles.cardTitle}>{savedRevision ? "View this revision" : current ? "View current project" : "Open linked project"}</span>
      <span className={styles.cardNote}>{savedRevision ? "Saved version from this response" : "Latest saved state · version not recorded for this message"}</span>
    </span>
    <ArrowUpRight className={styles.icon} />
  </>;
  return current && (!savedRevision || history)
    ? <button type="button" className={styles.card} onClick={() => {
      workspace?.openProject();
      if (savedRevision && history) { history.setOpen(true); history.select(savedRevision); }
      else if (history?.selection) history.returnToLatest();
    }} data-testid="project-update-card">{content}</button>
    : <a className={styles.card} href={linkedProjectPath(id, savedRevision)} data-testid="project-update-card">{content}</a>;
}

function makeBackgroundInert(pane: HTMLElement): () => void {
  const previous = new Map<HTMLElement, boolean>();
  let ancestor: HTMLElement | null = pane;
  while (ancestor && ancestor !== document.body) {
    const parent: HTMLElement | null = ancestor.parentElement;
    if (!parent) break;
    for (const sibling of Array.from(parent.children)) {
      if (sibling instanceof HTMLElement && sibling !== ancestor && !previous.has(sibling)) {
        previous.set(sibling, sibling.inert);
        sibling.inert = true;
      }
    }
    ancestor = parent;
  }
  return () => { previous.forEach((value, element) => { element.inert = value; }); };
}
