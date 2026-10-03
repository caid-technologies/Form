"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Clock3,
  Info,
  Database,
  Handshake,
  History,
  KeyRound,
  Layers,
  Menu,
  MessageSquare,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pin,
  Plus,
  RefreshCw,
  Settings,
  Terminal,
  Trash2,
  X,
} from "lucide-react";

import CaidLogo from "../../components/caid-logo";
import type { ChatActivity } from "../../lib/chat-activity";
import { FormaUserButton, useFormaAuth } from "../../lib/forma-auth";
import {
  type WorkspaceStatusPresentation,
  type WorkspaceStatusTone,
} from "../../lib/connection-status";

export type ChatListItem = {
  chatId: string;
  title: string;
  projectId: string;
  createdAt: string | null;
  projectCount: number;
  pinned?: boolean;
};

export function EditableWorkspaceTitle({
  value,
  canEdit,
  onCommit,
  label,
  className = "truncate text-sm font-semibold tracking-tight text-zinc-100",
  element = "h2",
}: {
  value: string;
  canEdit: boolean;
  onCommit: (nextTitle: string) => void;
  label: string;
  className?: string;
  element?: "h2" | "h3" | "div";
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const TitleTag = element;

  useEffect(() => {
    if (!editing) setDraft(value);
  }, [editing, value]);

  if (!canEdit) {
    return <TitleTag className={className}>{value}</TitleTag>;
  }

  if (editing) {
    return (
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          const next = draft.trim() || value.trim() || "Untitled Hardware Project";
          setEditing(false);
          if (next !== value) onCommit(next);
          else setDraft(value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            event.currentTarget.blur();
          }
          if (event.key === "Escape") {
            event.preventDefault();
            setDraft(value);
            setEditing(false);
          }
        }}
        autoFocus
        aria-label={label}
        className={`min-w-0 flex-1 bg-transparent ${className} outline-none`}
      />
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      title={`Rename ${label.toLowerCase()}`}
      aria-label={`Rename ${label.toLowerCase()}`}
      className={`min-w-0 flex-1 truncate text-left ${className} hover:text-white`}
    >
      {value}
    </button>
  );
}

function formatSidebarDate(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

const STATUS_BADGE_TONE_CLASS: Record<WorkspaceStatusTone, string> = {
  ok: "status-badge-ok",
  authoring: "status-badge-authoring",
  delivered: "status-badge-delivered",
  error: "status-badge-error",
};

function ApiConnectionStatus({ status }: { status: WorkspaceStatusPresentation }) {
  return (
    <span
      className={`status-badge ${STATUS_BADGE_TONE_CLASS[status.tone]} ${
        status.pulse ? "status-badge-pulse" : "status-badge-idle"
      }`}
      role="status"
      aria-live="polite"
      aria-label={status.label}
      title={status.label}
    >
      <span className="status-badge-ping" aria-hidden="true" />
      <span className="status-badge-dot" aria-hidden="true" />
    </span>
  );
}

export function WorkspaceStatusCorner({ status }: { status: WorkspaceStatusPresentation }) {
  return (
    <div className="pointer-events-none absolute right-4 top-4 z-40 flex items-center gap-2 overflow-visible">
      <span className="workspace-beta-badge">
        BETA
      </span>
      <span className="pointer-events-auto">
        <ApiConnectionStatus status={status} />
      </span>
    </div>
  );
}

export function MobileWorkspaceBar({
  onOpenSidebar,
  headerAway = false,
  children,
}: {
  onOpenSidebar: () => void;
  headerAway?: boolean;
  children?: ReactNode;
}) {
  return (
    <header className={`workspace-chrome-header absolute inset-x-0 top-0 z-20 flex min-h-14 min-w-0 items-center gap-3 px-3 pb-5 pt-2 sm:px-4 md:hidden ${headerAway ? "is-away" : ""}`}>
      <MobileSidebarButton onClick={onOpenSidebar} />
      {children}
    </header>
  );
}

export function AuthStatusControl({
  authRequired,
  compact = false,
}: {
  authRequired: boolean;
  compact?: boolean;
}) {
  const { isLoaded, isSignedIn, openSignIn } = useFormaAuth();
  if (!authRequired) return null;
  if (isSignedIn) return <FormaUserButton afterSignOutUrl="/" />;

  return (
    <button
      type="button"
      disabled={!isLoaded}
      onClick={() => openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" })}
      className={`inline-flex shrink-0 items-center justify-center rounded-lg border border-white/10 font-medium text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100 disabled:cursor-wait disabled:text-zinc-600 disabled:hover:bg-transparent ${
        compact ? "h-8 w-8" : "h-8 gap-1.5 px-2.5 text-xs"
      }`}
      aria-label={isLoaded ? "Sign in" : "Checking sign-in status"}
      title={isLoaded ? "Sign in" : "Checking sign-in status"}
    >
      <KeyRound className={compact ? "h-4 w-4" : "h-3.5 w-3.5"} />
      {!compact && <span>Sign in</span>}
    </button>
  );
}

export function MobileSidebarButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100 md:hidden"
      aria-label="Open sidebar"
      title="Open sidebar"
    >
      <Menu className="h-4 w-4" />
    </button>
  );
}

export function MobileSidebarDrawer({
  open,
  onClose,
  collapsed,
  onToggle,
  onHome,
  chats,
  activeChatId,
  onNewChat,
  newChatDisabled,
  newChatDisabledReason,
  readOnly,
  onOpenChat,
  onRenameChat,
  onPinChat,
  onDeleteChat,
  waitingChatIds,
  chatActivityById = {},
  chatsLoading,
  showJobs,
  jobsPending,
  showDeveloperTools,
  authRequired,
}: {
  open: boolean;
  onClose: () => void;
  collapsed: boolean;
  onToggle: () => void;
  onHome: () => void;
  chats: ChatListItem[];
  activeChatId: string | null;
  onNewChat: () => void;
  newChatDisabled: boolean;
  newChatDisabledReason?: string;
  readOnly?: boolean;
  onOpenChat: (item: ChatListItem) => void;
  onRenameChat?: (item: ChatListItem, title: string) => void;
  onPinChat?: (item: ChatListItem) => void;
  onDeleteChat?: (item: ChatListItem) => void;
  waitingChatIds: Set<string>;
  chatActivityById?: Record<string, ChatActivity>;
  chatsLoading?: boolean;
  showJobs: boolean;
  jobsPending?: boolean;
  showDeveloperTools: boolean;
  authRequired: boolean;
}) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Sidebar">
      <button
        type="button"
        className="absolute inset-0 h-full w-full bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close sidebar"
      />
      <div className="relative h-full">
        <ChatSidebar
          mode="drawer"
          collapsed={collapsed}
          onToggle={onToggle}
          onClose={onClose}
          onNavigate={onClose}
          onHome={onHome}
          chats={chats}
          activeChatId={activeChatId}
          onNewChat={onNewChat}
          newChatDisabled={newChatDisabled}
          newChatDisabledReason={newChatDisabledReason}
          readOnly={readOnly}
          onOpenChat={onOpenChat}
          onRenameChat={onRenameChat}
          onPinChat={onPinChat}
          onDeleteChat={onDeleteChat}
          waitingChatIds={waitingChatIds}
          chatActivityById={chatActivityById}
          chatsLoading={chatsLoading}
          showJobs={showJobs}
          jobsPending={jobsPending}
          showDeveloperTools={showDeveloperTools}
          authRequired={authRequired}
        />
      </div>
    </div>
  );
}

function ChatActivityIndicator({ activity, waiting, active, compact }: {
  activity?: ChatActivity;
  waiting: boolean;
  active: boolean;
  compact: boolean;
}) {
  const state = activity?.state || (waiting ? "running" : "settled");
  if (state === "settled") return null;
  const Icon = state === "running" || state === "reconnecting" ? RefreshCw
    : state === "queued" ? Clock3 : state === "checking" ? Info : AlertTriangle;
  return (
    <span role="status" aria-label={activity?.label || "Request in progress."} title={activity?.label || "Request in progress."}>
      <Icon className={`${compact ? "h-4 w-4" : "h-3.5 w-3.5"} shrink-0 ${state === "running" ? "animate-spin" : ""} ${
        state === "interrupted" || state === "reconnecting" ? "text-amber-500" : active ? "text-emerald-400" : "text-zinc-500"
      }`} aria-hidden="true" />
    </span>
  );
}

function ChatSidebarRow({
  chat,
  compact,
  active,
  waiting,
  activity,
  renaming,
  renameDraft,
  dateLabel,
  menuOpen,
  canRename,
  onRenameDraftChange,
  onCommitRename,
  onCancelRename,
  onStartRename,
  onOpen,
  onPin,
  onDelete,
  onToggleMenu,
  onCloseMenu,
  readOnly,
}: {
  chat: ChatListItem;
  compact: boolean;
  active: boolean;
  waiting: boolean;
  activity?: ChatActivity;
  renaming: boolean;
  renameDraft: string;
  dateLabel: string;
  menuOpen: boolean;
  canRename: boolean;
  onRenameDraftChange: (value: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
  onStartRename: () => void;
  onOpen: () => void;
  onPin?: () => void;
  onDelete?: () => void;
  onToggleMenu: () => void;
  onCloseMenu: () => void;
  readOnly: boolean;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; right: number } | null>(null);
  const showActions = Boolean(!compact && !readOnly && !renaming && (onPin || onDelete));
  const rowClassName = `group relative flex w-full min-w-0 items-center gap-1 rounded-lg text-left text-xs font-medium transition-colors ${
    active
      ? "bg-emerald-500/10 text-emerald-400"
      : "text-zinc-400 hover:bg-zinc-800/40 hover:text-zinc-200"
  } ${compact ? "justify-center px-0 py-2" : "pr-1"}`;

  useEffect(() => {
    if (!menuOpen) {
      setMenuPos(null);
      return;
    }
    const updatePosition = () => {
      const rect = menuRef.current?.getBoundingClientRect();
      if (!rect) return;
      const menuHeight = 88;
      const openUpward = rect.bottom + menuHeight > window.innerHeight - 8;
      setMenuPos({
        top: openUpward ? Math.max(8, rect.top - menuHeight - 4) : rect.bottom + 4,
        right: Math.max(8, window.innerWidth - rect.right),
      });
    };
    updatePosition();
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) onCloseMenu();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseMenu();
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", onCloseMenu);
    window.addEventListener("scroll", onCloseMenu, true);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", onCloseMenu);
      window.removeEventListener("scroll", onCloseMenu, true);
    };
  }, [menuOpen, onCloseMenu]);

  const titleBlock = compact ? (
    (waiting || activity) ? (
      <ChatActivityIndicator activity={activity} waiting={waiting} active={active} compact />
    ) : chat.pinned ? (
      <Pin className={`h-4 w-4 ${active ? "text-emerald-400" : "text-zinc-500"}`} />
    ) : (
      <MessageSquare className={`h-4 w-4 ${active ? "text-emerald-400" : "text-zinc-500"}`} />
    )
  ) : (
    <>
      <div className="min-w-0 flex-1">
        {renaming ? (
          <input
            value={renameDraft}
            onChange={(event) => onRenameDraftChange(event.target.value)}
            onBlur={onCommitRename}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                event.currentTarget.blur();
              }
              if (event.key === "Escape") {
                event.preventDefault();
                onCancelRename();
              }
            }}
            onClick={(event) => event.stopPropagation()}
            autoFocus
            aria-label={`Rename ${chat.title}`}
            className="w-full bg-transparent text-xs font-medium text-zinc-100 outline-none"
          />
        ) : (
          <div className="flex min-w-0 items-center gap-1.5">
            {chat.pinned && (
              <Pin className={`h-3 w-3 shrink-0 ${active ? "text-emerald-400" : "text-zinc-500"}`} aria-hidden="true" />
            )}
            <div className="truncate">{chat.title}</div>
          </div>
        )}
        {chat.projectCount > 1 && (
          <div className="mt-0.5 text-[10px] text-zinc-600">{chat.projectCount} projects</div>
        )}
      </div>
      {(waiting || activity) && (
        <ChatActivityIndicator activity={activity} waiting={waiting} active={active} compact={false} />
      )}
      {dateLabel && <div className="shrink-0 text-[10px] text-zinc-600">{dateLabel}</div>}
    </>
  );

  if (renaming) {
    return <div className={`${rowClassName} px-3 py-2`}>{titleBlock}</div>;
  }

  return (
    <div className={rowClassName}>
      <button
        type="button"
        onClick={onOpen}
        onDoubleClick={(event) => {
          if (compact || !canRename) return;
          event.preventDefault();
          event.stopPropagation();
          onStartRename();
        }}
        className={`flex min-w-0 flex-1 items-center gap-2.5 rounded-lg py-2 text-left ${compact ? "justify-center px-0" : "px-3"}`}
        title={activity ? `${chat.title}: ${activity.label}` : waiting ? `${chat.title} is waiting` : canRename ? `${chat.title}. Double-click to rename.` : chat.title}
        aria-label={`Open chat ${chat.title}${activity ? ` (${activity.label})` : waiting ? " (waiting)" : ""}${chat.pinned ? ", pinned" : ""}`}
      >
        {titleBlock}
      </button>
      {showActions && (
        <div ref={menuRef} className="relative shrink-0">
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onToggleMenu();
            }}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 opacity-0 transition-opacity hover:bg-zinc-800 hover:text-zinc-200 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 aria-expanded:opacity-100"
            aria-label={`More options for ${chat.title}`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
          {menuOpen && (
            <div
              role="menu"
              className="fixed z-50 w-36 overflow-hidden rounded-lg border border-white/10 bg-[#1f232c] py-1 shadow-xl shadow-black/50"
              style={menuPos ? { top: menuPos.top, right: menuPos.right } : { visibility: "hidden" }}
            >
              {onPin && (
                <button
                  type="button"
                  role="menuitem"
                  onClick={(event) => {
                    event.stopPropagation();
                    onCloseMenu();
                    onPin();
                  }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                >
                  <Pin className="h-3.5 w-3.5" />
                  {chat.pinned ? "Unpin" : "Pin"}
                </button>
              )}
              {onDelete && (
                <button
                  type="button"
                  role="menuitem"
                  onClick={(event) => {
                    event.stopPropagation();
                    onCloseMenu();
                    onDelete();
                  }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-red-300 transition-colors hover:bg-red-500/10 hover:text-red-200"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Delete
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SidebarSectionLabel({ compact, children }: { compact: boolean; children: React.ReactNode }) {
  if (compact) return null;
  return <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">{children}</div>;
}

function SidebarAccountDock({ compact }: { compact: boolean }) {
  const { isSignedIn } = useFormaAuth();
  return (
    <div className={`mt-3 flex items-center gap-2.5 border-t border-white/5 pt-3 ${compact ? "justify-center" : "px-3"}`}>
      <AuthStatusControl authRequired compact={compact || isSignedIn} />
      {!compact && isSignedIn && <span className="truncate text-xs font-medium text-zinc-500">Account</span>}
    </div>
  );
}

function SidebarNavLink({
  href,
  icon: Icon,
  label,
  compact,
  onNavigate,
}: {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  compact: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-xs font-medium text-zinc-400 transition-colors hover:bg-zinc-800/40 hover:text-zinc-200 ${
        compact ? "justify-center px-0" : ""
      }`}
      title={label}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {!compact && <span className="truncate">{label}</span>}
    </Link>
  );
}

export function ChatSidebar({
  collapsed,
  onToggle,
  onClose,
  onNavigate,
  onHome,
  chats,
  activeChatId,
  onNewChat,
  newChatDisabled,
  newChatDisabledReason,
  readOnly = false,
  onOpenChat,
  onRenameChat,
  onPinChat,
  onDeleteChat,
  waitingChatIds,
  chatActivityById = {},
  chatsLoading = false,
  showJobs,
  jobsPending = false,
  showDeveloperTools,
  authRequired,
  mode = "desktop",
}: {
  collapsed: boolean;
  onToggle: () => void;
  onClose?: () => void;
  onNavigate?: () => void;
  onHome: () => void;
  chats: ChatListItem[];
  activeChatId: string | null;
  onNewChat: () => void;
  newChatDisabled: boolean;
  newChatDisabledReason?: string;
  readOnly?: boolean;
  onOpenChat: (item: ChatListItem) => void;
  onRenameChat?: (item: ChatListItem, title: string) => void;
  onPinChat?: (item: ChatListItem) => void;
  onDeleteChat?: (item: ChatListItem) => void;
  waitingChatIds: Set<string>;
  chatActivityById?: Record<string, ChatActivity>;
  chatsLoading?: boolean;
  showJobs: boolean;
  jobsPending?: boolean;
  showDeveloperTools: boolean;
  authRequired: boolean;
  mode?: "desktop" | "drawer";
}) {
  const isDrawer = mode === "drawer";
  const compact = !isDrawer && collapsed;
  const [renamingChatId, setRenamingChatId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [menuChatId, setMenuChatId] = useState<string | null>(null);

  const commitSidebarRename = (chat: ChatListItem) => {
    const nextTitle = renameDraft.trim() || chat.title;
    setRenamingChatId(null);
    if (nextTitle !== chat.title) onRenameChat?.(chat, nextTitle);
  };

  return (
    <aside
      className={
        isDrawer
          ? "flex h-full min-h-0 w-[min(320px,calc(100vw-2rem))] flex-col overflow-hidden rounded-r-2xl border-r border-white/5 bg-[#181b22] text-zinc-100 shadow-2xl shadow-black/50"
          : "hidden h-full min-h-0 flex-col border-r border-white/5 bg-[#181b22] text-zinc-100 md:flex"
      }
    >
      <div className="flex min-h-0 flex-1 flex-col">
        <div className={`flex shrink-0 items-center ${compact ? "h-auto flex-col justify-center gap-2 px-0 py-3" : "h-14 gap-2 px-3"}`}>
          <button
            type="button"
            onClick={() => {
              onHome();
              onNavigate?.();
            }}
            className="inline-flex h-9 w-11 shrink-0 items-center justify-center rounded-lg text-zinc-200 transition-opacity hover:opacity-80 focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-2 focus-visible:outline-emerald-500"
            aria-label="Home"
            title="Home"
          >
            <CaidLogo className="h-5 w-9" sizes="36px" />
          </button>
          {!compact && (
            <span className="min-w-0 truncate text-sm font-semibold tracking-tight text-zinc-100">Forma</span>
          )}
          <button
            type="button"
            onClick={isDrawer ? onClose : onToggle}
            className={`${compact ? "h-8 w-8" : "ml-auto h-8 w-8"} inline-flex shrink-0 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:bg-zinc-800/40 hover:text-zinc-200`}
            aria-label={isDrawer ? "Close sidebar" : compact ? "Expand chat sidebar" : "Collapse chat sidebar"}
            title={isDrawer ? "Close sidebar" : compact ? "Expand sidebar" : "Collapse sidebar"}
          >
            {isDrawer ? <X className="h-4 w-4" /> : compact ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </button>
        </div>

        <div className="px-3 pb-2 pt-1">
          <button
            type="button"
            onClick={() => {
              onNewChat();
              if (!newChatDisabled) onNavigate?.();
            }}
            disabled={newChatDisabled}
            className={`flex h-9 w-full items-center justify-center gap-2 rounded-lg text-xs font-semibold transition-all focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ${
              newChatDisabled
                ? "cursor-not-allowed bg-zinc-800/40 text-zinc-600"
                : "forma-action-fill shadow-sm"
            } ${compact ? "px-0" : "px-3"}`}
            aria-label="New chat"
            title={newChatDisabled ? newChatDisabledReason || "Send a message before starting another chat" : "New chat"}
          >
            <Plus className="h-4 w-4 shrink-0" />
            {!compact && <span className="truncate">New chat</span>}
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
          <SidebarSectionLabel compact={compact}>Chats</SidebarSectionLabel>
          <div className="space-y-0.5">
            {chatsLoading ? (
              Array.from({ length: compact ? 5 : 7 }, (_, index) => (
                <div
                  key={`chat-skeleton-${index}`}
                  className={`flex h-9 animate-pulse items-center gap-3 px-3 ${compact ? "justify-center" : ""}`}
                  aria-hidden="true"
                >
                  {compact ? (
                    <span className="h-4 w-4 rounded bg-zinc-800/80" />
                  ) : (
                    <>
                      <span className="h-2.5 flex-1 rounded bg-zinc-800/80" />
                      <span className="h-2.5 w-9 rounded bg-zinc-800/60" />
                    </>
                  )}
                </div>
              ))
            ) : chats.length ? (
              chats.map((chat) => (
                <ChatSidebarRow
                  key={chat.chatId}
                  chat={chat}
                  compact={compact}
                  active={chat.chatId === activeChatId}
                  waiting={waitingChatIds.has(chat.chatId)}
                  activity={chatActivityById[chat.chatId]}
                  renaming={!readOnly && !compact && Boolean(onRenameChat) && renamingChatId === chat.chatId}
                  renameDraft={renameDraft}
                  dateLabel={formatSidebarDate(chat.createdAt)}
                  menuOpen={menuChatId === chat.chatId}
                  canRename={!readOnly && !compact && Boolean(onRenameChat)}
                  onRenameDraftChange={setRenameDraft}
                  onCommitRename={() => commitSidebarRename(chat)}
                  onCancelRename={() => {
                    setRenameDraft(chat.title);
                    setRenamingChatId(null);
                  }}
                  onStartRename={() => {
                    setMenuChatId(null);
                    setRenameDraft(chat.title);
                    setRenamingChatId(chat.chatId);
                  }}
                  onOpen={() => {
                    setMenuChatId(null);
                    onOpenChat(chat);
                    onNavigate?.();
                  }}
                  onPin={!readOnly && onPinChat ? () => onPinChat(chat) : undefined}
                  onDelete={!readOnly && onDeleteChat ? () => onDeleteChat(chat) : undefined}
                  onToggleMenu={() => setMenuChatId((current) => (current === chat.chatId ? null : chat.chatId))}
                  onCloseMenu={() => setMenuChatId(null)}
                  readOnly={readOnly}
                />
              ))
            ) : (
              !compact && <div className="px-3 py-2 text-xs leading-5 text-zinc-500">No saved chats yet.</div>
            )}
          </div>
        </div>
      </div>

      <div className="border-t border-white/5 px-3 py-3">
        <SidebarSectionLabel compact={compact}>Workspace</SidebarSectionLabel>
        <div className="space-y-0.5">
          <SidebarNavLink href="/my-projects" icon={Database} label="My projects" compact={compact} onNavigate={onNavigate} />
          <SidebarNavLink href="/projects" icon={Layers} label="Community" compact={compact} onNavigate={onNavigate} />
          {showJobs ? (
            <SidebarNavLink href="/jobs" icon={History} label="Jobs" compact={compact} onNavigate={onNavigate} />
          ) : jobsPending ? (
            <div className={`flex h-8 animate-pulse items-center gap-2.5 px-3 ${compact ? "justify-center px-0" : ""}`} aria-hidden="true">
              <span className="h-4 w-4 rounded bg-zinc-800/80" />
              {!compact && <span className="h-2.5 w-14 rounded bg-zinc-800/80" />}
            </div>
          ) : null}
          {showDeveloperTools && (
            <SidebarNavLink href="/backend-logs" icon={Terminal} label="Backend logs" compact={compact} onNavigate={onNavigate} />
          )}
          {showDeveloperTools && (
            <SidebarNavLink href="/listening-jobs" icon={Terminal} label="Listening jobs" compact={compact} onNavigate={onNavigate} />
          )}
        </div>
        <SidebarSectionLabel compact={compact}>General</SidebarSectionLabel>
        <div className={`space-y-0.5 ${compact ? "mt-1" : ""}`}>
          <SidebarNavLink href="/settings" icon={Settings} label="Settings" compact={compact} onNavigate={onNavigate} />
          <SidebarNavLink href="/install/opencode" icon={Terminal} label="OpenCode setup" compact={compact} onNavigate={onNavigate} />
          <SidebarNavLink href="/about" icon={Handshake} label="About" compact={compact} onNavigate={onNavigate} />
        </div>
        {authRequired && <SidebarAccountDock compact={compact} />}
      </div>
    </aside>
  );
}
