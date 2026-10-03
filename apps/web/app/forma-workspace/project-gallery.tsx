"use client";

import React, { useEffect, useMemo, useState } from "react";
import Image from "next/image";
import {
  ArrowLeft,
  ArrowRight,
  Bookmark,
  Clock3,
  GitFork,
  MessageSquare,
  Search,
  X,
} from "lucide-react";

import {
  previewableImageSrc,
  resolveProjectImageCandidates,
  type ProjectImageCandidate,
} from "../../lib/project-images";

export type ProjectGalleryItem = {
  key: string;
  title: string;
  projectId: string;
  chatId: string;
  canChat: boolean;
  creatorDisplay: string;
  creatorImageUrl: string | null;
  createdAt: string | null;
  partsCount: number;
  saveCount: number;
  remixCount: number;
  saved: boolean;
  image: ProjectImageCandidate | null;
  imageLoading: boolean;
};

// Keep pagination stable across server render and hydration. The grid itself is
// responsive; changing the item count after mount caused the whole page to jump.
export const PROJECT_GALLERY_PAGE_SIZE = 6;

export {
  previewableImageSrc,
  resolveProjectImageCandidates,
  type ProjectImageCandidate,
};

function formatProjectAge(value: string | null) {
  if (!value) return "";
  const createdAt = new Date(value).getTime();
  if (Number.isNaN(createdAt)) return "";
  const elapsedMs = Math.max(0, Date.now() - createdAt);
  const hours = Math.floor(elapsedMs / (60 * 60 * 1000));
  if (hours < 24) return hours <= 0 ? "Today" : `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 31) return days === 1 ? "1 day" : `${days} days`;
  const months = Math.floor(days / 30);
  return months === 1 ? "1 month" : `${months} months`;
}

export function buildProjectGalleryItems(
  projectHistory: any[],
  projectImages: Record<string, ProjectImageCandidate | null>,
  showModelName = false,
): ProjectGalleryItem[] {
  return projectHistory
    .filter((project: any) => project?.project_id)
    .map((project: any) => {
      const projectId = String(project.project_id);
      const chatId = String(project.chat_id || projectId);
      const summaryImage =
        resolveProjectImageCandidates({
          product_visual_sequence: project.product_visual_sequence,
          product_image_url: project.product_image_url,
          product_image_data: project.product_image_data,
          product_image_content_type: project.product_image_content_type,
          product_image_model: project.product_image_model,
          image_output_model: project.image_output_model,
        }, showModelName)[0] || null;
      const fetchedImage = projectImages[projectId];
      return {
        key: projectId,
        title: project.title || "Untitled project",
        projectId,
        chatId,
        canChat: Boolean(project.can_chat ?? project.canChat),
        creatorDisplay:
          typeof project.creator_username === "string" && project.creator_username.trim()
            ? project.creator_username.trim()
            : typeof project.creator_display === "string" && project.creator_display.trim()
              ? project.creator_display.trim()
              : "unknown",
        creatorImageUrl:
          typeof project.creator_image_url === "string" && project.creator_image_url.trim()
            ? project.creator_image_url.trim()
            : typeof project.creatorImageUrl === "string" && project.creatorImageUrl.trim()
              ? project.creatorImageUrl.trim()
              : null,
        createdAt: typeof project.created_at === "string" && project.created_at ? project.created_at : null,
        partsCount: Math.max(0, Number(project.parts_count || project.partsCount || 0)),
        saveCount: Math.max(0, Number(project.save_count || project.saveCount || 0)),
        remixCount: Math.max(0, Number(project.remix_count || project.remixCount || 0)),
        saved: Boolean(project.saved),
        image: fetchedImage || summaryImage,
        imageLoading: !summaryImage && fetchedImage === undefined,
      };
    });
}

export function ProjectGallery({
  sectionRef,
  items,
  title = "Projects",
  loading = false,
  onOpenProjectPage,
  onToggleSave,
  onRemixProject,
  onVisibleProjectIdsChange,
  totalItems,
  currentPage: controlledPage,
  onPageChange,
  searchValue,
  onSearchValueChange,
  standalone = false,
}: {
  sectionRef: React.RefObject<HTMLElement | null>;
  items: ProjectGalleryItem[];
  title?: string;
  loading?: boolean;
  onOpenProjectPage: (projectId: string) => void;
  onToggleSave?: (item: ProjectGalleryItem) => void | Promise<void>;
  onRemixProject?: (item: ProjectGalleryItem) => void | Promise<void>;
  onVisibleProjectIdsChange?: (projectIds: string[]) => void;
  totalItems?: number;
  currentPage?: number;
  onPageChange?: (page: number) => void;
  searchValue?: string;
  onSearchValueChange?: (value: string) => void;
  standalone?: boolean;
}) {
  const pageSize = PROJECT_GALLERY_PAGE_SIZE;
  const [localPage, setLocalPage] = useState(0);
  const serverPaginated = typeof totalItems === "number" && typeof controlledPage === "number" && Boolean(onPageChange);
  const currentPage = serverPaginated ? controlledPage : localPage;
  const total = serverPaginated ? Math.max(0, totalItems) : items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(currentPage, pageCount - 1);
  const firstVisibleItem = safePage * pageSize;
  const visibleItems = useMemo(
    () => serverPaginated ? items : items.slice(firstVisibleItem, firstVisibleItem + pageSize),
    [firstVisibleItem, items, pageSize, serverPaginated]
  );
  const visibleProjectIds = useMemo(
    () => visibleItems.map((item) => item.projectId),
    [visibleItems]
  );
  const showingStart = total ? firstVisibleItem + 1 : 0;
  const showingEnd = Math.min(total, firstVisibleItem + visibleItems.length);
  const pageMarkers = buildProjectGalleryPageMarkers(safePage, pageCount);
  const searchable = typeof searchValue === "string" && Boolean(onSearchValueChange);
  const hasSearch = Boolean(searchValue?.trim());

  useEffect(() => {
    if (!serverPaginated) setLocalPage(0);
  }, [items.length, pageSize, serverPaginated]);

  useEffect(() => {
    if (safePage !== currentPage) {
      if (serverPaginated) onPageChange?.(safePage);
      else setLocalPage(safePage);
    }
  }, [currentPage, onPageChange, safePage, serverPaginated]);

  useEffect(() => {
    onVisibleProjectIdsChange?.(visibleProjectIds);
  }, [onVisibleProjectIdsChange, visibleProjectIds]);

  const goToPage = (page: number) => {
    const nextPage = Math.min(Math.max(page, 0), pageCount - 1);
    if (serverPaginated) onPageChange?.(nextPage);
    else setLocalPage(nextPage);
  };

  return (
    <section ref={sectionRef} id="all-projects" className={standalone ? "" : "mt-16 border-t border-white/5 pt-12"}>
      <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="sr-only">{title}</h2>
          <p className="text-sm leading-6 text-zinc-500">
            {loading
              ? hasSearch ? "Searching projects..." : "Loading projects..."
              : hasSearch ? `${total} matching projects.` : `${total} saved projects.`}
          </p>
        </div>
        {searchable && (
          <label className="relative block w-full sm:max-w-sm">
            <span className="sr-only">Search community projects</span>
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
            <input
              type="search"
              value={searchValue}
              onChange={(event) => onSearchValueChange?.(event.target.value)}
              placeholder="Search projects"
              className="h-9 w-full rounded-lg border border-white/5 bg-[#181b22] pl-10 pr-10 text-sm text-zinc-100 outline-none transition placeholder:text-zinc-500 focus:border-emerald-500"
            />
            {hasSearch && (
              <button
                type="button"
                onClick={() => onSearchValueChange?.("")}
                className="absolute right-0.5 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg text-zinc-500 transition-colors hover:text-zinc-100"
                aria-label="Clear project search"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </label>
        )}
      </div>

      {loading ? (
        <ProjectGallerySkeleton count={pageSize} />
      ) : total && visibleItems.length ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {visibleItems.map((item) => (
              <ProjectGalleryCard
                key={item.key}
                item={item}
                onOpen={() => onOpenProjectPage(item.projectId)}
                onToggleSave={onToggleSave ? () => onToggleSave(item) : undefined}
                onRemix={onRemixProject ? () => onRemixProject(item) : undefined}
              />
            ))}
          </div>

          {pageCount > 1 && (
            <div className="mt-5 flex flex-col gap-3 rounded-xl border border-white/5 bg-[#181b22] p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="text-xs font-medium text-zinc-500">
                Showing {showingStart}-{showingEnd} of {total}
              </div>

              <div className="grid grid-cols-[40px_minmax(0,1fr)_40px] gap-2 sm:flex sm:items-center">
                <button
                  type="button"
                  onClick={() => goToPage(safePage - 1)}
                  disabled={safePage === 0}
                  className="inline-flex h-8 w-9 items-center justify-center rounded-lg border border-white/10 text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                  aria-label="Previous projects page"
                >
                  <ArrowLeft className="h-4 w-4" />
                </button>

                <div className="hidden items-center gap-1.5 sm:flex">
                  {pageMarkers.map((marker, index) => (
                    marker === "gap" ? (
                      <span
                        key={`gap-${index}`}
                        className="flex h-8 min-w-6 items-center justify-center text-xs font-medium text-zinc-600"
                      >
                        ...
                      </span>
                    ) : (
                      <button
                        key={marker}
                        type="button"
                        onClick={() => goToPage(marker)}
                        aria-current={marker === safePage ? "page" : undefined}
                        className={`h-8 min-w-8 rounded-lg border px-2.5 text-xs font-medium transition-colors ${
                          marker === safePage
                            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                            : "border-transparent text-zinc-400 hover:bg-zinc-800/40 hover:text-zinc-200"
                        }`}
                      >
                        {marker + 1}
                      </button>
                    )
                  ))}
                </div>

                <div className="flex h-8 items-center justify-center rounded-lg border border-white/10 text-xs font-medium text-zinc-400 sm:hidden">
                  Page {safePage + 1} / {pageCount}
                </div>

                <button
                  type="button"
                  onClick={() => goToPage(safePage + 1)}
                  disabled={safePage >= pageCount - 1}
                  className="inline-flex h-8 w-9 items-center justify-center rounded-lg border border-white/10 text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                  aria-label="Next projects page"
                >
                  <ArrowRight className="h-4 w-4" />
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="rounded-xl border border-white/5 bg-[#181b22] p-8 text-sm leading-6 text-zinc-500">
          {hasSearch ? `No projects match “${searchValue?.trim()}”.` : "No saved projects yet."}
        </div>
      )}
    </section>
  );
}

function buildProjectGalleryPageMarkers(currentPage: number, pageCount: number): Array<number | "gap"> {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index);
  }

  const markers = new Set([0, pageCount - 1, currentPage]);

  if (currentPage > 0) {
    markers.add(currentPage - 1);
  }

  if (currentPage < pageCount - 1) {
    markers.add(currentPage + 1);
  }

  const sortedMarkers = Array.from(markers).sort((a, b) => a - b);
  return sortedMarkers.flatMap((marker, index) => {
    const previousMarker = sortedMarkers[index - 1];
    return index > 0 && marker - previousMarker > 1 ? ["gap", marker] : [marker];
  });
}

function ProjectGallerySkeleton({ count }: { count: number }) {
  const skeletonItems = Array.from({ length: Math.max(1, count) }, (_, index) => index);
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Loading projects">
      {skeletonItems.map((item) => (
        <div
          key={item}
          className="overflow-hidden rounded-xl border border-white/5 bg-[#181b22]"
        >
          <div className="aspect-square overflow-hidden border-b border-white/5 bg-[#0f1117] sm:aspect-[4/3]">
            <ProjectImageLoadingPanel />
          </div>
          <div className="flex min-h-[150px] flex-col justify-between gap-3 p-4">
            <div className="space-y-2">
              <div className="h-4 w-4/5 animate-pulse rounded bg-[#252832]" />
              <div className="h-4 w-3/5 animate-pulse rounded bg-[#252832]" />
            </div>
            <div className="flex gap-4">
              <div className="h-3 w-14 animate-pulse rounded bg-[#252832]" />
              <div className="h-3 w-10 animate-pulse rounded bg-[#252832]" />
              <div className="h-3 w-12 animate-pulse rounded bg-[#252832]" />
            </div>
            <div className="flex items-center justify-between gap-3">
              <div className="h-4 w-24 animate-pulse rounded bg-[#252832]" />
              <div className="h-7 w-24 animate-pulse rounded-full bg-[#252832]" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ProjectImageLoadingPanel() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-5 text-center">
      <div className="h-1.5 w-28 overflow-hidden rounded-full bg-[#252832]">
        <div className="h-full w-full animate-pulse rounded-full bg-emerald-500/60" />
      </div>
      <div className="text-xs font-medium text-zinc-500">
        Loading image
      </div>
    </div>
  );
}

function ProjectGalleryPlaceholderThumb() {
  return (
    <Image
      src="/project-placeholder.jpg"
      alt=""
      width={1}
      height={1}
      unoptimized
      className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.015]"
    />
  );
}

function formatGalleryMetricCount(count: number) {
  if (count < 10) return null;
  if (count < 1000) return String(count);
  if (count < 10_000) {
    const compact = Math.round(count / 100) / 10;
    return `${compact.toString().replace(/\.0$/, "")}k`;
  }
  if (count < 1_000_000) return `${Math.round(count / 1000)}k`;
  const compact = Math.round(count / 100_000) / 10;
  return `${compact.toString().replace(/\.0$/, "")}M`;
}

function ProjectGalleryMetric({
  icon: Icon,
  count,
  label,
  active = false,
  interactive = false,
  busy = false,
  pressed,
  onClick,
}: {
  icon: React.ComponentType<{ className?: string }>;
  count: number;
  label: string;
  active?: boolean;
  interactive?: boolean;
  busy?: boolean;
  pressed?: boolean;
  onClick?: () => void;
}) {
  const visibleCount = formatGalleryMetricCount(count);
  const className = `inline-flex items-center gap-1 whitespace-nowrap ${
    active ? "text-emerald-400" : "text-zinc-500"
  }`;
  const content = (
    <>
      <Icon className={`h-3.5 w-3.5 ${active ? "fill-current" : ""}`} />
      {visibleCount ? <span className="tabular-nums">{visibleCount}</span> : null}
    </>
  );
  if (!interactive || !onClick) {
    return (
      <span className={className} title={`${count} ${label}`}>
        {content}
        <span className="sr-only">{`${count} ${label}`}</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      disabled={busy}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      onKeyDown={(event) => event.stopPropagation()}
      className={`${className} rounded-md transition-colors hover:text-zinc-200 disabled:opacity-50`}
      aria-label={label}
      aria-pressed={typeof pressed === "boolean" ? pressed : undefined}
      title={`${count} ${label}`}
    >
      {content}
    </button>
  );
}

function ProjectGalleryCard({
  item,
  onOpen,
  onToggleSave,
  onRemix,
}: {
  item: ProjectGalleryItem;
  onOpen: () => void;
  onToggleSave?: () => void | Promise<void>;
  onRemix?: () => void | Promise<void>;
}) {
  const ageLabel = formatProjectAge(item.createdAt);
  const [saveBusy, setSaveBusy] = useState(false);
  const [remixBusy, setRemixBusy] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const interactive = Boolean(onToggleSave || onRemix);

  useEffect(() => {
    setImageFailed(false);
  }, [item.image?.src]);

  const handleSave = async () => {
    if (!onToggleSave || saveBusy) return;
    setSaveBusy(true);
    try {
      await onToggleSave();
    } finally {
      setSaveBusy(false);
    }
  };

  const handleRemix = async () => {
    if (!onRemix || remixBusy) return;
    setRemixBusy(true);
    try {
      await onRemix();
    } finally {
      setRemixBusy(false);
    }
  };

  return (
    <article
      role="link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen();
        }
      }}
      className="group cursor-pointer overflow-hidden rounded-xl border border-white/5 bg-[#181b22] outline-none transition-all hover:border-zinc-700/60 focus-visible:border-emerald-500"
      aria-label={`View project ${item.title}`}
    >
      <div className="aspect-square overflow-hidden border-b border-white/5 bg-[#0f1117] sm:aspect-[4/3]">
        {item.image && !imageFailed ? (
          <Image
            src={item.image.src}
            alt={`${item.title} preview`}
            width={1}
            height={1}
            unoptimized
            className="h-full w-full object-contain p-2 transition duration-300 group-hover:scale-[1.015] sm:object-cover sm:p-0"
            onError={() => setImageFailed(true)}
          />
        ) : item.imageLoading && !imageFailed ? (
          <ProjectImageLoadingPanel />
        ) : (
          <ProjectGalleryPlaceholderThumb />
        )}
      </div>

      <div className="flex min-h-[150px] flex-col justify-between gap-3 p-4">
        <div className="flex min-w-0 items-start gap-2">
          <h3 className="min-h-10 min-w-0 flex-1 line-clamp-2 break-words text-sm font-medium leading-5 text-zinc-100">
            {item.title}
          </h3>
          <div className="flex shrink-0 items-center gap-2 pt-0.5 text-xs font-medium">
            <ProjectGalleryMetric
              icon={Bookmark}
              count={item.saveCount}
              label={item.saved ? "saves" : interactive ? "Save project" : "saves"}
              active={item.saved}
              pressed={item.saved}
              interactive={Boolean(onToggleSave)}
              busy={saveBusy}
              onClick={handleSave}
            />
            <ProjectGalleryMetric
              icon={GitFork}
              count={item.remixCount}
              label={interactive ? "Remix project" : "remixes"}
              interactive={Boolean(onRemix)}
              busy={remixBusy}
              onClick={handleRemix}
            />
          </div>
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 text-xs font-medium text-zinc-500">
          <span className="whitespace-nowrap">{item.partsCount} parts</span>
          {ageLabel && (
            <span className="inline-flex items-center gap-1 whitespace-nowrap">
              <Clock3 className="h-3.5 w-3.5" />
              {ageLabel}
            </span>
          )}
        </div>
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2 text-xs font-medium text-zinc-500">
            {item.creatorImageUrl ? (
              <Image
                src={item.creatorImageUrl}
                alt=""
                width={20}
                height={20}
                unoptimized
                className="h-5 w-5 shrink-0 rounded-full border border-white/10 object-cover"
              />
            ) : (
              <span className="h-3.5 w-3.5 shrink-0 rounded-full bg-emerald-400 shadow-[inset_6px_0_0_#f472b6]" />
            )}
            <span className="truncate">{item.creatorDisplay}</span>
          </div>
          {item.canChat && (
            <span className="inline-flex h-7 shrink-0 items-center justify-center gap-1.5 rounded-full bg-emerald-500/10 px-2.5 text-xs font-medium text-emerald-400 transition-colors">
              <MessageSquare className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">Your project</span>
            </span>
          )}
        </div>
      </div>
    </article>
  );
}
