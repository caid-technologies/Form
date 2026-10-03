"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { VideoGenerationMode, VideoModelOption } from "./use-video-models";

export const VIDEO_PROMPT_MAX_CHARS = 2500;
export const DEFAULT_VIDEO_POLL_INTERVAL_MS = 4000;
const VIDEO_STATUS_REQUEST_TIMEOUT_MS = 20000;

export type StoredVideoInfo = {
  bucket?: string;
  key?: string;
  s3Uri?: string;
  publicUrl?: string | null;
  signedUrl?: string | null;
  url?: string | null;
  contentType?: string;
  sizeBytes?: number;
  metadata?: Record<string, unknown>;
};

export type ProjectVideoImageOption = {
  src: string;
  label: string;
  viewId?: string;
  prompt?: string;
  dimensions?: string;
};

export type VideoFeatureAvailability = {
  configured: boolean | null;
  reason: string | null;
};

export type ProjectVideoModelControls = {
  models: VideoModelOption[];
  loading: boolean;
  error: string | null;
  selectedModel: string;
  setSelectedModel: (value: string) => void;
  aspectRatios: string[];
  aspectRatio: string;
  setAspectRatio: (value: string) => void;
};

export type ProjectVideoHeaderFactory = () => HeadersInit | Promise<HeadersInit>;
export type ProjectVideoErrorReader = (response: Response) => Promise<string>;

export type UseProjectVideoOptions<ProjectIR = unknown> = {
  apiUrl: string;
  /** Enables the lazy saved-video request. Polling already in progress continues. */
  enabled: boolean;
  projectId: string | null;
  /** Opaque, stable identity key. Change it whenever the authenticated user changes. */
  authIdentityKey: string | null;
  canManageProject: boolean;
  canLoadProjectVideos: boolean;
  imageOptions: ProjectVideoImageOption[];
  defaultImage?: string;
  authorizeGeneration: () => boolean | Promise<boolean>;
  getRequestHeaders: ProjectVideoHeaderFactory;
  readError: ProjectVideoErrorReader;
  modelControls: ProjectVideoModelControls;
  generationAvailability: VideoFeatureAvailability;
  reviewAvailability: VideoFeatureAvailability;
  globalBusy: boolean;
  setGlobalBusy: (busy: boolean) => void;
  updateProject: (projectIR: ProjectIR | null, response: unknown) => void;
  refreshProjectAndChatLists: () => void;
  refreshJobs: () => void;
  pollIntervalMs?: number;
};

export type UseProjectVideoResult = {
  projectId: string | null;
  readOnly: boolean;
  canOpenAssets: boolean;
  models: VideoModelOption[];
  modelsLoading: boolean;
  modelsError: string | null;
  selectedModel: string;
  setSelectedModel: (value: string) => void;
  mode: VideoGenerationMode;
  setMode: Dispatch<SetStateAction<VideoGenerationMode>>;
  imageInput: string;
  setImageInput: (value: string) => void;
  imageOptions: ProjectVideoImageOption[];
  selectedImageSources: string[];
  setSelectedImageSources: Dispatch<SetStateAction<string[]>>;
  defaultImage: string;
  sourceVideoUrl: string;
  setSourceVideoUrl: Dispatch<SetStateAction<string>>;
  prompt: string;
  setPrompt: Dispatch<SetStateAction<string>>;
  duration: string;
  setDuration: Dispatch<SetStateAction<string>>;
  aspectRatio: string;
  setAspectRatio: (value: string) => void;
  aspectRatios: string[];
  status: string;
  statusMessage: string | null;
  requestId: string | null;
  storedVideo: StoredVideoInfo | null;
  gallery: StoredVideoInfo[];
  galleryLoading: boolean;
  galleryError: string | null;
  generationAvailable: boolean;
  generationUnavailableReason: string | null;
  reviewStatus: string;
  reviewMessage: string | null;
  reviewAvailable: boolean;
  reviewUnavailableReason: string | null;
  selectedReviewVideoKey: string | null;
  setSelectedReviewVideoKey: Dispatch<SetStateAction<string | null>>;
  selectedReviewVideo: StoredVideoInfo | null;
  reviewableVideoUrl: string;
  makeNewVideo: boolean;
  setMakeNewVideo: Dispatch<SetStateAction<boolean>>;
  promptGenerating: boolean;
  promptMessage: string | null;
  onGenerate: () => Promise<void>;
  onGeneratePrompt: () => Promise<void>;
  onReview: () => Promise<void>;
  onReviewVideo: (video: StoredVideoInfo) => Promise<void>;
  onUploadImage: () => void;
  onUseProjectImage: () => void;
  onRefreshGallery: () => Promise<void>;
  canGenerate: boolean;
  canReview: boolean;
  canMakeNewVideo: boolean;
  canGeneratePrompt: boolean;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onImageFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
};

type VideoPollContext = {
  model: string;
  mode: VideoGenerationMode;
  prompt: string;
  sourceUrl: string;
  aspectRatio: string;
};

type VideoPollTarget = VideoPollContext & {
  requestId: string;
  projectId: string;
  authIdentityKey: string | null;
  runId: number;
};

type GalleryLoadOptions = {
  silent?: boolean;
};

type VideoStateScope = {
  projectId: string | null;
  authIdentityKey: string | null;
  canManageProject: boolean;
  canLoadProjectVideos: boolean;
};

export function videoIdentity(
  video: StoredVideoInfo | null | undefined,
  fallback = ""
): string {
  return video?.key || video?.s3Uri || video?.url || video?.publicUrl || video?.signedUrl || fallback;
}

export function videoSourceUrl(video: StoredVideoInfo | null | undefined): string {
  return video?.url || video?.publicUrl || video?.signedUrl || "";
}

export function videoMetadataString(
  video: StoredVideoInfo | null | undefined,
  keys: string[]
): string {
  const metadata = video?.metadata || {};
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }

  const lowered = new Map(
    Object.entries(metadata).map(([key, value]) => [key.toLowerCase(), value])
  );
  for (const key of keys) {
    const value = lowered.get(key.toLowerCase());
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function videoPromptText(video: StoredVideoInfo | null | undefined): string {
  return videoMetadataString(video, [
    "prompt",
    "videoPrompt",
    "video_prompt",
    "promptPreview",
    "prompt_preview",
  ]);
}

export function fitVideoPromptForProvider(prompt: string, suffix = ""): string {
  const cleanPrompt = String(prompt || "").trim();
  const cleanSuffix = String(suffix || "");
  if (!cleanSuffix) {
    return cleanPrompt.length > VIDEO_PROMPT_MAX_CHARS
      ? cleanPrompt.slice(0, VIDEO_PROMPT_MAX_CHARS).trimEnd()
      : cleanPrompt;
  }
  if (cleanSuffix.length >= VIDEO_PROMPT_MAX_CHARS) {
    return cleanSuffix.slice(0, VIDEO_PROMPT_MAX_CHARS).trimEnd();
  }
  const availablePromptChars = VIDEO_PROMPT_MAX_CHARS - cleanSuffix.length;
  const fittedPrompt = cleanPrompt.length > availablePromptChars
    ? cleanPrompt.slice(0, availablePromptChars).trimEnd()
    : cleanPrompt;
  return `${fittedPrompt}${cleanSuffix}`;
}

export function videoPromptWasTrimmed(original: string, fitted: string): boolean {
  return String(original || "").trim().length > fitted.length;
}

export function videoLabel(
  video: StoredVideoInfo | null | undefined,
  fallback = "video"
): string {
  return videoMetadataString(video, ["requestId", "request_id"])
    || video?.key?.split("/").pop()
    || fallback;
}

export function mergeStoredVideoGallery(
  current: StoredVideoInfo[],
  incoming: StoredVideoInfo[]
): StoredVideoInfo[] {
  const byKey = new Map<string, StoredVideoInfo>();
  [...incoming, ...current].forEach((video, index) => {
    const key = videoIdentity(video, `video-${index}`);
    byKey.set(key, video);
  });
  return Array.from(byKey.values());
}

export function isFinalVideoStatus(status: string): boolean {
  return [
    "succeeded",
    "success",
    "completed",
    "complete",
    "done",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
  ].includes(status);
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

/**
 * Owns the project-scoped video workflow. Model discovery deliberately remains
 * in useVideoModels; its stable state is supplied through modelControls.
 */
export function useProjectVideo<ProjectIR = unknown>({
  apiUrl,
  enabled,
  projectId,
  authIdentityKey,
  canManageProject,
  canLoadProjectVideos,
  imageOptions,
  defaultImage = "",
  authorizeGeneration,
  getRequestHeaders,
  readError,
  modelControls,
  generationAvailability,
  reviewAvailability,
  globalBusy,
  setGlobalBusy,
  updateProject,
  refreshProjectAndChatLists,
  refreshJobs,
  pollIntervalMs = DEFAULT_VIDEO_POLL_INTERVAL_MS,
}: UseProjectVideoOptions<ProjectIR>): UseProjectVideoResult {
  const {
    models,
    loading: modelsLoading,
    error: modelsError,
    selectedModel,
    setSelectedModel,
    aspectRatios,
    aspectRatio,
    setAspectRatio,
  } = modelControls;

  const [imageInput, setImageInputState] = useState("");
  const [selectedImageSources, setSelectedImageSources] = useState<string[]>([]);
  const [imageTouched, setImageTouched] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [promptGenerating, setPromptGenerating] = useState(false);
  const [promptMessage, setPromptMessage] = useState<string | null>(null);
  const [duration, setDuration] = useState("5");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [status, setStatus] = useState("idle");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [mode, setMode] = useState<VideoGenerationMode>("image-to-video");
  const [sourceVideoUrl, setSourceVideoUrl] = useState("");
  const [storedVideo, setStoredVideo] = useState<StoredVideoInfo | null>(null);
  const [gallery, setGallery] = useState<StoredVideoInfo[]>([]);
  const [galleryLoading, setGalleryLoading] = useState(false);
  const [galleryError, setGalleryError] = useState<string | null>(null);
  const [selectedReviewVideoKey, setSelectedReviewVideoKey] = useState<string | null>(null);
  const [makeNewVideo, setMakeNewVideo] = useState(false);
  const [reviewStatus, setReviewStatus] = useState("idle");
  const [reviewMessage, setReviewMessage] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const currentProjectIdRef = useRef(projectId);
  currentProjectIdRef.current = projectId;
  const currentAuthIdentityKeyRef = useRef(authIdentityKey);
  currentAuthIdentityKeyRef.current = authIdentityKey;
  const canManageProjectRef = useRef(canManageProject);
  canManageProjectRef.current = canManageProject;
  const canLoadProjectVideosRef = useRef(canLoadProjectVideos);
  canLoadProjectVideosRef.current = canLoadProjectVideos;
  const globalBusyRef = useRef(globalBusy);
  globalBusyRef.current = globalBusy;
  const videoGenerationBusy = status === "loading"
    || Boolean(requestId && !storedVideo && !isFinalVideoStatus(status));
  const videoGenerationBusyRef = useRef(videoGenerationBusy);
  videoGenerationBusyRef.current = videoGenerationBusy;
  const setGlobalBusyRef = useRef(setGlobalBusy);
  setGlobalBusyRef.current = setGlobalBusy;
  const stateScopeRef = useRef<VideoStateScope>({
    projectId,
    authIdentityKey,
    canManageProject,
    canLoadProjectVideos,
  });
  const loadedGalleryProjectRef = useRef<string | null>(null);
  const galleryRequestProjectRef = useRef<string | null>(null);
  const galleryRequestIdRef = useRef(0);
  const galleryControllerRef = useRef<AbortController | null>(null);
  const promptRequestIdRef = useRef(0);
  const promptControllerRef = useRef<AbortController | null>(null);
  const generationControllerRef = useRef<AbortController | null>(null);
  const reviewControllerRef = useRef<AbortController | null>(null);
  const reviewRequestIdRef = useRef(0);
  const activeRunIdRef = useRef(0);
  const activePollTargetRef = useRef<VideoPollTarget | null>(null);
  const statusRequestIdsRef = useRef(new Map<string, number>());
  const statusControllersRef = useRef(new Map<string, AbortController>());
  const delayedPollsRef = useRef(new Set<number>());

  const imageOptionSources = useMemo(
    () => imageOptions.map((candidate) => candidate.src),
    [imageOptions]
  );
  const reviewableVideos = useMemo(
    () => gallery.filter((video) => Boolean(videoSourceUrl(video))),
    [gallery]
  );
  const selectedReviewVideo = useMemo(() => {
    const selected = selectedReviewVideoKey
      ? reviewableVideos.find(
          (video, index) => videoIdentity(video, `video-${index}`) === selectedReviewVideoKey
        )
      : null;
    return selected
      || (videoSourceUrl(storedVideo) ? storedVideo : null)
      || reviewableVideos[0]
      || null;
  }, [reviewableVideos, selectedReviewVideoKey, storedVideo]);
  const reviewableVideoUrl = useMemo(
    () => videoSourceUrl(selectedReviewVideo),
    [selectedReviewVideo]
  );

  const abortStatusRequests = useCallback(() => {
    statusControllersRef.current.forEach((controller) => controller.abort());
    statusControllersRef.current.clear();
    statusRequestIdsRef.current.clear();
    delayedPollsRef.current.forEach((timeoutId) => window.clearTimeout(timeoutId));
    delayedPollsRef.current.clear();
    activePollTargetRef.current = null;
  }, []);

  const beginVideoRun = useCallback(() => {
    activeRunIdRef.current += 1;
    generationControllerRef.current?.abort();
    generationControllerRef.current = null;
    abortStatusRequests();
    return activeRunIdRef.current;
  }, [abortStatusRequests]);

  const fetchProjectVideos = useCallback(async (
    targetProjectId: string,
    options: GalleryLoadOptions = {}
  ) => {
    if (
      !targetProjectId
      || currentProjectIdRef.current !== targetProjectId
      || !canLoadProjectVideosRef.current
    ) {
      return;
    }

    const targetAuthIdentityKey = currentAuthIdentityKeyRef.current;
    const galleryRequestId = ++galleryRequestIdRef.current;
    galleryControllerRef.current?.abort();
    const controller = new AbortController();
    galleryControllerRef.current = controller;
    galleryRequestProjectRef.current = targetProjectId;
    setGalleryLoading(!options.silent);
    setGalleryError(null);

    try {
      const response = await fetch(
        `${apiUrl}/video/projects/${encodeURIComponent(targetProjectId)}`,
        {
          headers: await getRequestHeaders(),
          signal: controller.signal,
        }
      );
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json();

      if (
        controller.signal.aborted
        || galleryRequestIdRef.current !== galleryRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canLoadProjectVideosRef.current
      ) {
        return;
      }

      const videos: StoredVideoInfo[] = Array.isArray(data?.videos) ? data.videos : [];
      setGallery(videos);
      loadedGalleryProjectRef.current = targetProjectId;
    } catch (error) {
      if (
        isAbortError(error)
        || galleryRequestIdRef.current !== galleryRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canLoadProjectVideosRef.current
      ) {
        return;
      }
      setGalleryError(error instanceof Error ? error.message : "Video gallery is unavailable.");
    } finally {
      if (galleryRequestIdRef.current === galleryRequestId) {
        if (galleryControllerRef.current === controller) galleryControllerRef.current = null;
        if (galleryRequestProjectRef.current === targetProjectId) {
          galleryRequestProjectRef.current = null;
        }
        setGalleryLoading(false);
      }
    }
  }, [apiUrl, getRequestHeaders, readError]);

  const applyVideoStatusResponse = useCallback((data: unknown, primary = true) => {
    const payload = data as {
      storedVideo?: StoredVideoInfo | null;
      savedVideos?: StoredVideoInfo[];
      status?: string;
    } | null;
    const saved = payload?.storedVideo
      || (Array.isArray(payload?.savedVideos) ? payload.savedVideos[0] : null);
    const statusValue = typeof payload?.status === "string"
      ? payload.status
      : saved
        ? "succeeded"
        : "queued";

    if (saved) {
      setGallery((current) => mergeStoredVideoGallery(current, [saved]));
      const savedKey = videoIdentity(saved);
      if (savedKey) setSelectedReviewVideoKey(savedKey);
      if (!primary) return;
      setStoredVideo(saved);
      setStatus("succeeded");
      setStatusMessage("Video saved.");
      return;
    }

    if (!primary) return;
    setStoredVideo(null);
    setStatus(statusValue);
    setStatusMessage(statusValue === "queued" ? "Queued." : `Status: ${statusValue}.`);
  }, []);

  const pollVideoStatus = useCallback(async (explicitTarget?: VideoPollTarget | null) => {
    const target = explicitTarget || activePollTargetRef.current;
    if (
      !target
      || currentProjectIdRef.current !== target.projectId
      || currentAuthIdentityKeyRef.current !== target.authIdentityKey
      || !canManageProjectRef.current
      || activeRunIdRef.current !== target.runId
    ) {
      return;
    }

    const requestKey = [
      target.projectId,
      target.authIdentityKey || "",
      String(target.runId),
      target.requestId,
    ].join("\u0000");
    // A slow status endpoint must be allowed to finish; starting another poll
    // would otherwise abort/starve it on every interval tick.
    if (statusControllersRef.current.has(requestKey)) return;
    const nextRequestId = (statusRequestIdsRef.current.get(requestKey) || 0) + 1;
    statusRequestIdsRef.current.set(requestKey, nextRequestId);
    const controller = new AbortController();
    statusControllersRef.current.set(requestKey, controller);
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      VIDEO_STATUS_REQUEST_TIMEOUT_MS
    );

    const params = new URLSearchParams({
      projectId: target.projectId,
      model: target.model,
      mode: target.mode,
    });
    if (target.prompt) params.set("prompt", target.prompt);
    if (target.aspectRatio) params.set("aspectRatio", target.aspectRatio);
    if (target.sourceUrl) params.set("sourceUrl", target.sourceUrl);

    try {
      const response = await fetch(
        `${apiUrl}/video/image-to-video/status/${encodeURIComponent(target.requestId)}?${params.toString()}`,
        {
          headers: await getRequestHeaders(),
          signal: controller.signal,
        }
      );
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json();

      if (
        controller.signal.aborted
        || statusRequestIdsRef.current.get(requestKey) !== nextRequestId
        || currentProjectIdRef.current !== target.projectId
        || currentAuthIdentityKeyRef.current !== target.authIdentityKey
        || !canManageProjectRef.current
        || activeRunIdRef.current !== target.runId
      ) {
        return;
      }

      const activeTarget = activePollTargetRef.current;
      const primary = Boolean(
        activeTarget
        && activeTarget.projectId === target.projectId
        && activeTarget.authIdentityKey === target.authIdentityKey
        && activeTarget.runId === target.runId
        && activeTarget.requestId === target.requestId
      );
      applyVideoStatusResponse(data, primary);
    } catch (error) {
      if (
        isAbortError(error)
        || statusRequestIdsRef.current.get(requestKey) !== nextRequestId
        || currentProjectIdRef.current !== target.projectId
        || currentAuthIdentityKeyRef.current !== target.authIdentityKey
        || !canManageProjectRef.current
        || activeRunIdRef.current !== target.runId
      ) {
        return;
      }
      const activeTarget = activePollTargetRef.current;
      if (
        activeTarget?.projectId === target.projectId
        && activeTarget.authIdentityKey === target.authIdentityKey
        && activeTarget.runId === target.runId
        && activeTarget.requestId === target.requestId
      ) {
        setStatus("failed");
        setStatusMessage(error instanceof Error ? error.message : "Network request failed.");
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (statusControllersRef.current.get(requestKey) === controller) {
        statusControllersRef.current.delete(requestKey);
      }
    }
  }, [apiUrl, applyVideoStatusResponse, getRequestHeaders, readError]);

  const scheduleStatusPoll = useCallback((target: VideoPollTarget, delayMs: number) => {
    const timeoutId = window.setTimeout(() => {
      delayedPollsRef.current.delete(timeoutId);
      void pollVideoStatus(target);
    }, delayMs);
    delayedPollsRef.current.add(timeoutId);
  }, [pollVideoStatus]);

  const generatePrompt = useCallback(async () => {
    const targetProjectId = currentProjectIdRef.current;
    if (!targetProjectId || !canManageProjectRef.current) {
      setPromptMessage("Open a generated project before creating a video prompt.");
      return;
    }
    const targetAuthIdentityKey = currentAuthIdentityKeyRef.current;

    const promptRequestId = ++promptRequestIdRef.current;
    promptControllerRef.current?.abort();
    const controller = new AbortController();
    promptControllerRef.current = controller;
    setPromptGenerating(true);
    setPromptMessage("Generating prompt from project namespaces.");
    try {
      const response = await fetch(
        `${apiUrl}/projects/${encodeURIComponent(targetProjectId)}/video-prompt`,
        {
          headers: await getRequestHeaders(),
          signal: controller.signal,
        }
      );
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json();
      if (
        controller.signal.aborted
        || promptRequestIdRef.current !== promptRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canManageProjectRef.current
      ) {
        return;
      }

      const nextPrompt = typeof data?.prompt === "string" ? data.prompt.trim() : "";
      if (!nextPrompt) throw new Error("The project did not return a usable video prompt.");
      const fittedPrompt = fitVideoPromptForProvider(nextPrompt);
      const providerMax = Number(data?.prompt_max_chars || VIDEO_PROMPT_MAX_CHARS);
      const wasTrimmed = Boolean(data?.prompt_truncated)
        || videoPromptWasTrimmed(nextPrompt, fittedPrompt);
      setMode("image-to-video");
      setPrompt(fittedPrompt);
      const namespaceCount = Array.isArray(data?.namespaces) ? data.namespaces.length : 0;
      setPromptMessage(
        [
          namespaceCount
            ? `Prompt generated from ${namespaceCount} namespaces.`
            : "Prompt generated from project namespaces.",
          wasTrimmed
            ? `Trimmed to fit the video provider prompt limit (${providerMax} chars).`
            : "",
        ].filter(Boolean).join(" ")
      );
    } catch (error) {
      if (
        isAbortError(error)
        || promptRequestIdRef.current !== promptRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canManageProjectRef.current
      ) {
        return;
      }
      setPromptMessage(
        error instanceof Error ? error.message : "Video prompt generation failed."
      );
    } finally {
      if (
        promptRequestIdRef.current === promptRequestId
        && currentProjectIdRef.current === targetProjectId
        && currentAuthIdentityKeyRef.current === targetAuthIdentityKey
      ) {
        if (promptControllerRef.current === controller) promptControllerRef.current = null;
        setPromptGenerating(false);
      }
    }
  }, [apiUrl, getRequestHeaders, readError]);

  const generate = useCallback(async () => {
    if (!(await authorizeGeneration())) return;
    if (
      !canManageProjectRef.current
      || globalBusyRef.current
      || reviewControllerRef.current
    ) return;
    if (generationAvailability.configured === false) {
      setStatus("idle");
      setStatusMessage("Video generation is coming soon.");
      return;
    }

    const targetProjectId = currentProjectIdRef.current;
    const targetAuthIdentityKey = currentAuthIdentityKeyRef.current;
    const manualImage = imageInput.trim();
    const selectedProjectImages = selectedImageSources.filter((source) => source.trim());
    const images = selectedProjectImages.length
      ? selectedProjectImages
      : manualImage
        ? [manualImage]
        : [];
    const sourceVideo = sourceVideoUrl.trim();
    const rawPromptText = prompt.trim();
    const promptText = fitVideoPromptForProvider(rawPromptText);
    const model = selectedModel.trim();
    const isVideoToVideo = mode === "video-to-video";

    if (
      !targetProjectId
      || !promptText
      || !model
      || (isVideoToVideo ? !sourceVideo : !images.length)
    ) {
      setStatus("failed");
      setStatusMessage(
        `Project id, ${isVideoToVideo ? "source video" : "image"}, prompt, and model are required.`
      );
      return;
    }
    if (videoPromptWasTrimmed(rawPromptText, promptText)) {
      setPrompt(promptText);
      setPromptMessage(
        `Prompt trimmed to the video provider limit (${VIDEO_PROMPT_MAX_CHARS} chars).`
      );
    }

    const runId = beginVideoRun();
    const controller = new AbortController();
    generationControllerRef.current = controller;
    setRequestId(null);
    setStoredVideo(null);
    setStatus("loading");
    setStatusMessage("Starting.");
    setReviewStatus("idle");
    setReviewMessage(null);

    try {
      const sources = isVideoToVideo ? [sourceVideo] : images;
      for (let index = 0; index < sources.length; index += 1) {
        if (
          controller.signal.aborted
          || activeRunIdRef.current !== runId
          || currentProjectIdRef.current !== targetProjectId
          || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
          || !canManageProjectRef.current
        ) {
          return;
        }

        const source = sources[index];
        const selectedImage = !isVideoToVideo
          ? imageOptions.find((candidate) => candidate.src === source)
          : null;
        const sourceViewSuffix = selectedImage?.label
          ? `\nSource view: ${selectedImage.label}.`
          : "";
        const viewPrompt = fitVideoPromptForProvider(promptText, sourceViewSuffix);
        if (
          sourceViewSuffix
          && videoPromptWasTrimmed(`${promptText}${sourceViewSuffix}`, viewPrompt)
        ) {
          setPromptMessage(
            `Prompt trimmed so the selected image label fits the provider limit (${VIDEO_PROMPT_MAX_CHARS} chars).`
          );
        }
        setStatusMessage(
          sources.length > 1 ? `Starting ${index + 1} of ${sources.length}.` : "Starting."
        );

        const response = await fetch(
          `${apiUrl}/video/${isVideoToVideo ? "video-to-video" : "image-to-video"}`,
          {
            method: "POST",
            headers: await getRequestHeaders(),
            body: JSON.stringify({
              projectId: targetProjectId,
              ...(isVideoToVideo ? { video: source } : { image: source }),
              prompt: viewPrompt,
              model,
              duration,
              aspectRatio,
              sound: "off",
            }),
            signal: controller.signal,
          }
        );
        if (!response.ok) throw new Error(await readError(response));
        const data = await response.json();
        if (
          controller.signal.aborted
          || activeRunIdRef.current !== runId
          || currentProjectIdRef.current !== targetProjectId
          || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
          || !canManageProjectRef.current
        ) {
          return;
        }

        const nextRequestId = typeof data?.requestId === "string" ? data.requestId : null;
        setRequestId(nextRequestId);
        let pollTarget: VideoPollTarget | null = null;
        if (nextRequestId) {
          pollTarget = {
            requestId: nextRequestId,
            projectId: targetProjectId,
            authIdentityKey: targetAuthIdentityKey,
            runId,
            model,
            mode,
            prompt: viewPrompt,
            sourceUrl: source,
            aspectRatio,
          };
          activePollTargetRef.current = pollTarget;
        }
        applyVideoStatusResponse(data, true);
        if (pollTarget && !data?.storedVideo) {
          scheduleStatusPoll(pollTarget, 800 + index * 400);
        }
      }

      setStatusMessage(sources.length > 1 ? `Queued ${sources.length} video requests.` : null);
      void fetchProjectVideos(targetProjectId, { silent: true });
    } catch (error) {
      if (
        isAbortError(error)
        || activeRunIdRef.current !== runId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canManageProjectRef.current
      ) {
        return;
      }
      setStatus("failed");
      setStatusMessage(error instanceof Error ? error.message : "Network request failed.");
    } finally {
      if (generationControllerRef.current === controller) {
        generationControllerRef.current = null;
      }
    }
  }, [
    apiUrl,
    applyVideoStatusResponse,
    aspectRatio,
    authorizeGeneration,
    beginVideoRun,
    duration,
    fetchProjectVideos,
    generationAvailability.configured,
    getRequestHeaders,
    imageInput,
    imageOptions,
    mode,
    prompt,
    readError,
    scheduleStatusPoll,
    selectedImageSources,
    selectedModel,
    sourceVideoUrl,
  ]);

  const reviewVideo = useCallback(async (targetVideo?: StoredVideoInfo) => {
    if (!(await authorizeGeneration())) return;
    if (!canManageProjectRef.current || videoGenerationBusyRef.current) return;
    if (reviewAvailability.configured === false) {
      setReviewStatus("failed");
      setReviewMessage(reviewAvailability.reason || "Video review is not configured.");
      return;
    }

    const targetProjectId = currentProjectIdRef.current;
    const targetAuthIdentityKey = currentAuthIdentityKeyRef.current;
    const reviewTarget = targetVideo || selectedReviewVideo;
    const targetKey = videoIdentity(reviewTarget);
    const videoUrl = (videoSourceUrl(reviewTarget) || reviewableVideoUrl).trim();
    if (!targetProjectId || !videoUrl || !reviewTarget) {
      setReviewStatus("failed");
      setReviewMessage("A project and saved video with a reviewable URL are required.");
      return;
    }
    if (targetKey) setSelectedReviewVideoKey(targetKey);

    const reviewRequestId = ++reviewRequestIdRef.current;
    reviewControllerRef.current?.abort();
    const controller = new AbortController();
    reviewControllerRef.current = controller;
    setGlobalBusy(true);
    setReviewStatus("loading");
    setReviewMessage(
      makeNewVideo
        ? "Reviewing video. New video will queue after correction."
        : "Reviewing video."
    );

    try {
      const response = await fetch(
        `${apiUrl}/projects/${encodeURIComponent(targetProjectId)}/video-self-correct`,
        {
          method: "POST",
          headers: await getRequestHeaders(),
          body: JSON.stringify({
            video_url: videoUrl,
            video_key: reviewTarget.key || null,
            save: true,
          }),
          signal: controller.signal,
        }
      );
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json();
      if (
        controller.signal.aborted
        || reviewRequestIdRef.current !== reviewRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canManageProjectRef.current
      ) {
        return;
      }

      updateProject((data?.project_ir ?? null) as ProjectIR | null, data);
      const review = data?.video_review || {};
      const issueCount = Array.isArray(review.issues) ? review.issues.length : 0;
      const summary = typeof review.summary === "string"
        ? review.summary
        : "Video review iteration applied.";
      const nextReviewMessage = issueCount
        ? `${summary} ${issueCount} issue${issueCount === 1 ? "" : "s"} applied.`
        : summary;
      setReviewStatus("succeeded");
      setReviewMessage(nextReviewMessage);
      refreshProjectAndChatLists();
      refreshJobs();

      if (makeNewVideo) {
        if (generationAvailability.configured === false) {
          const message = generationAvailability.reason || "Video generation is not configured.";
          setStatus("failed");
          setStatusMessage(message);
          setReviewMessage(`${nextReviewMessage} New video was not queued: ${message}`);
          return;
        }

        const nextModel = models.find(
          (candidate) => candidate.mode === "video-to-video" && candidate.id === selectedModel
        )?.id || models.find((candidate) => candidate.mode === "video-to-video")?.id || "";
        if (!nextModel) {
          const message = "No video-to-video model is available for a new iteration.";
          setStatus("failed");
          setStatusMessage(message);
          setReviewMessage(`${nextReviewMessage} New video was not queued: ${message}`);
          return;
        }

        const savedPrompt = videoPromptText(reviewTarget);
        const rawCorrectionPrompt = [
          savedPrompt || prompt.trim() || "Create a corrected hardware product video iteration.",
          summary ? `Correction guidance: ${summary}` : "",
        ].filter(Boolean).join("\n");
        const correctionPrompt = fitVideoPromptForProvider(rawCorrectionPrompt);
        const correctionPromptTrimmed = videoPromptWasTrimmed(
          rawCorrectionPrompt,
          correctionPrompt
        );

        const runId = beginVideoRun();
        setMode("video-to-video");
        setSourceVideoUrl(videoUrl);
        setSelectedModel(nextModel);
        if (correctionPrompt) setPrompt(correctionPrompt);
        setRequestId(null);
        setStoredVideo(null);
        setStatus("loading");
        setStatusMessage(
          correctionPromptTrimmed
            ? "Starting corrected video with a trimmed prompt."
            : "Starting corrected video."
        );

        try {
          const videoResponse = await fetch(`${apiUrl}/video/video-to-video`, {
            method: "POST",
            headers: await getRequestHeaders(),
            body: JSON.stringify({
              projectId: targetProjectId,
              video: videoUrl,
              prompt: correctionPrompt,
              model: nextModel,
              duration,
              aspectRatio,
              sound: "off",
            }),
            signal: controller.signal,
          });
          if (!videoResponse.ok) throw new Error(await readError(videoResponse));
          const videoData = await videoResponse.json();
          if (
            controller.signal.aborted
            || reviewRequestIdRef.current !== reviewRequestId
            || currentProjectIdRef.current !== targetProjectId
            || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
            || !canManageProjectRef.current
            || activeRunIdRef.current !== runId
          ) {
            return;
          }

          const nextRequestId = typeof videoData?.requestId === "string"
            ? videoData.requestId
            : null;
          setRequestId(nextRequestId);
          let pollTarget: VideoPollTarget | null = null;
          if (nextRequestId) {
            pollTarget = {
              requestId: nextRequestId,
              projectId: targetProjectId,
              authIdentityKey: targetAuthIdentityKey,
              runId,
              model: nextModel,
              mode: "video-to-video",
              prompt: correctionPrompt,
              sourceUrl: videoUrl,
              aspectRatio,
            };
            activePollTargetRef.current = pollTarget;
          }
          applyVideoStatusResponse(videoData, true);
          setReviewMessage(`${nextReviewMessage} New video queued from the selected card.`);
          if (pollTarget && !videoData?.storedVideo) scheduleStatusPoll(pollTarget, 800);
          void fetchProjectVideos(targetProjectId, { silent: true });
        } catch (videoError) {
          if (
            isAbortError(videoError)
            || reviewRequestIdRef.current !== reviewRequestId
            || currentProjectIdRef.current !== targetProjectId
            || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
            || !canManageProjectRef.current
          ) {
            return;
          }
          const message = videoError instanceof Error
            ? videoError.message
            : "New video request failed.";
          setStatus("failed");
          setStatusMessage(message);
          setReviewMessage(`${nextReviewMessage} New video was not queued: ${message}`);
        }
      }
    } catch (error) {
      if (
        isAbortError(error)
        || reviewRequestIdRef.current !== reviewRequestId
        || currentProjectIdRef.current !== targetProjectId
        || currentAuthIdentityKeyRef.current !== targetAuthIdentityKey
        || !canManageProjectRef.current
      ) {
        return;
      }
      setReviewStatus("failed");
      setReviewMessage(error instanceof Error ? error.message : "Video review failed.");
    } finally {
      if (reviewRequestIdRef.current === reviewRequestId) {
        if (reviewControllerRef.current === controller) reviewControllerRef.current = null;
        setGlobalBusy(false);
      }
    }
  }, [
    apiUrl,
    applyVideoStatusResponse,
    aspectRatio,
    authorizeGeneration,
    beginVideoRun,
    duration,
    fetchProjectVideos,
    generationAvailability.configured,
    generationAvailability.reason,
    getRequestHeaders,
    makeNewVideo,
    models,
    prompt,
    readError,
    refreshJobs,
    refreshProjectAndChatLists,
    reviewAvailability.configured,
    reviewAvailability.reason,
    reviewableVideoUrl,
    scheduleStatusPoll,
    selectedModel,
    selectedReviewVideo,
    setGlobalBusy,
    setSelectedModel,
    updateProject,
  ]);

  const setImageInput = useCallback((value: string) => {
    setImageTouched(true);
    setImageInputState(value);
  }, []);

  const onImageFileChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onloadend = () => {
      if (typeof reader.result !== "string") return;
      setImageTouched(true);
      setImageInputState(reader.result);
      setSelectedImageSources([]);
      setStatusMessage(null);
    };
    reader.readAsDataURL(file);
  }, []);

  const onUseProjectImage = useCallback(() => {
    setImageTouched(false);
    const nextSource = imageOptions[0]?.src || defaultImage;
    setImageInputState(nextSource);
    setSelectedImageSources(nextSource ? [nextSource] : []);
  }, [defaultImage, imageOptions]);

  const refreshGallery = useCallback(async () => {
    const targetProjectId = currentProjectIdRef.current;
    if (!targetProjectId || !canLoadProjectVideos) return;
    await fetchProjectVideos(targetProjectId);
  }, [canLoadProjectVideos, fetchProjectVideos]);

  useEffect(() => {
    const previousScope = stateScopeRef.current;
    const accessChanged = previousScope.authIdentityKey !== authIdentityKey
      || previousScope.canManageProject !== canManageProject
      || previousScope.canLoadProjectVideos !== canLoadProjectVideos;
    stateScopeRef.current = {
      projectId,
      authIdentityKey,
      canManageProject,
      canLoadProjectVideos,
    };

    galleryRequestIdRef.current += 1;
    galleryControllerRef.current?.abort();
    galleryControllerRef.current = null;
    galleryRequestProjectRef.current = null;
    loadedGalleryProjectRef.current = null;
    promptRequestIdRef.current += 1;
    promptControllerRef.current?.abort();
    promptControllerRef.current = null;
    generationControllerRef.current?.abort();
    generationControllerRef.current = null;
    reviewRequestIdRef.current += 1;
    if (reviewControllerRef.current) setGlobalBusyRef.current(false);
    reviewControllerRef.current?.abort();
    reviewControllerRef.current = null;
    activeRunIdRef.current += 1;
    abortStatusRequests();

    setImageTouched(false);
    setRequestId(null);
    setStoredVideo(null);
    setGallery([]);
    setGalleryLoading(false);
    setGalleryError(null);
    setSelectedReviewVideoKey(null);
    setSourceVideoUrl("");
    setSelectedImageSources([]);
    setMode("image-to-video");
    setPromptMessage(null);
    setPromptGenerating(false);
    setStatus("idle");
    setStatusMessage(null);
    setReviewStatus("idle");
    setReviewMessage(null);
    setMakeNewVideo(false);
    if (accessChanged) {
      setImageInputState("");
      setPrompt("");
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [
    abortStatusRequests,
    authIdentityKey,
    canLoadProjectVideos,
    canManageProject,
    projectId,
  ]);

  useEffect(() => {
    if (!enabled || !projectId || !canLoadProjectVideos) return;
    if (
      loadedGalleryProjectRef.current === projectId
      || galleryRequestProjectRef.current === projectId
    ) {
      return;
    }
    void fetchProjectVideos(projectId);
  }, [authIdentityKey, canLoadProjectVideos, enabled, fetchProjectVideos, projectId]);

  useEffect(() => {
    const keys = reviewableVideos
      .map((video, index) => videoIdentity(video, `video-${index}`))
      .filter(Boolean);
    setSelectedReviewVideoKey((current) => (
      current && keys.includes(current) ? current : keys[0] || null
    ));
  }, [reviewableVideos]);

  useEffect(() => {
    setSelectedImageSources((current) => {
      const retained = current.filter((source) => imageOptionSources.includes(source));
      if (retained.length) return retained;
      return imageOptionSources[0] ? [imageOptionSources[0]] : [];
    });
    if (!imageTouched) setImageInputState(imageOptionSources[0] || defaultImage);
  }, [defaultImage, imageOptionSources, imageTouched]);

  useEffect(() => {
    const modeModels = models.filter((candidate) => candidate.mode === mode);
    if (modeModels.some((candidate) => candidate.id === selectedModel)) return;
    setSelectedModel(modeModels[0]?.id || "");
  }, [mode, models, selectedModel, setSelectedModel]);

  useEffect(() => {
    const sourceVideos = gallery.map(videoSourceUrl).filter(Boolean);
    if (!sourceVideos.length) {
      setSourceVideoUrl("");
      if (mode === "video-to-video") setMode("image-to-video");
      return;
    }
    setSourceVideoUrl((current) => (
      current && sourceVideos.includes(current) ? current : sourceVideos[0]
    ));
  }, [gallery, mode]);

  useEffect(() => {
    if (!requestId || storedVideo || isFinalVideoStatus(status)) return;
    const target = activePollTargetRef.current;
    if (!target || target.requestId !== requestId) return;

    const intervalId = window.setInterval(() => {
      void pollVideoStatus(target);
    }, pollIntervalMs);
    return () => window.clearInterval(intervalId);
  }, [pollIntervalMs, pollVideoStatus, requestId, status, storedVideo]);

  useEffect(() => () => {
    galleryRequestIdRef.current += 1;
    galleryControllerRef.current?.abort();
    promptRequestIdRef.current += 1;
    promptControllerRef.current?.abort();
    generationControllerRef.current?.abort();
    reviewRequestIdRef.current += 1;
    reviewControllerRef.current?.abort();
    abortStatusRequests();
  }, [abortStatusRequests]);

  const generationAvailable = generationAvailability.configured !== false;
  const reviewAvailable = reviewAvailability.configured !== false;
  const stateScopeMatches = stateScopeRef.current.projectId === projectId
    && stateScopeRef.current.authIdentityKey === authIdentityKey
    && stateScopeRef.current.canManageProject === canManageProject
    && stateScopeRef.current.canLoadProjectVideos === canLoadProjectVideos;
  const canGenerate = Boolean(
    stateScopeMatches
    && canManageProject
    && !globalBusy
    && reviewStatus !== "loading"
    && generationAvailable
    && projectId
    && prompt.trim()
    && selectedModel
    && (mode === "video-to-video"
      ? sourceVideoUrl.trim()
      : selectedImageSources.length > 0 || imageInput.trim())
  );
  const canReview = Boolean(
    stateScopeMatches
    && canManageProject
    && reviewAvailable
    && projectId
    && selectedReviewVideo
    && reviewableVideoUrl
    && reviewStatus !== "loading"
    && !videoGenerationBusy
    && !globalBusy
  );

  return {
    projectId,
    readOnly: !canManageProject,
    canOpenAssets: stateScopeMatches && canLoadProjectVideos,
    models,
    modelsLoading,
    modelsError,
    selectedModel,
    setSelectedModel,
    mode: stateScopeMatches ? mode : "image-to-video",
    setMode,
    imageInput: stateScopeMatches ? imageInput : "",
    setImageInput,
    imageOptions,
    selectedImageSources: stateScopeMatches ? selectedImageSources : [],
    setSelectedImageSources,
    defaultImage,
    sourceVideoUrl: stateScopeMatches ? sourceVideoUrl : "",
    setSourceVideoUrl,
    prompt: stateScopeMatches ? prompt : "",
    setPrompt,
    duration,
    setDuration,
    aspectRatio,
    setAspectRatio,
    aspectRatios,
    status: stateScopeMatches ? status : "idle",
    statusMessage: stateScopeMatches ? statusMessage : null,
    requestId: stateScopeMatches ? requestId : null,
    storedVideo: stateScopeMatches && canLoadProjectVideos ? storedVideo : null,
    gallery: stateScopeMatches && canLoadProjectVideos ? gallery : [],
    galleryLoading: stateScopeMatches ? galleryLoading : false,
    galleryError: stateScopeMatches ? galleryError : null,
    generationAvailable,
    generationUnavailableReason: generationAvailability.reason,
    reviewStatus: stateScopeMatches ? reviewStatus : "idle",
    reviewMessage: stateScopeMatches ? reviewMessage : null,
    reviewAvailable,
    reviewUnavailableReason: reviewAvailability.reason,
    selectedReviewVideoKey: stateScopeMatches ? selectedReviewVideoKey : null,
    setSelectedReviewVideoKey,
    selectedReviewVideo: stateScopeMatches ? selectedReviewVideo : null,
    reviewableVideoUrl: stateScopeMatches ? reviewableVideoUrl : "",
    makeNewVideo: stateScopeMatches ? makeNewVideo : false,
    setMakeNewVideo,
    promptGenerating: stateScopeMatches ? promptGenerating : false,
    promptMessage: stateScopeMatches ? promptMessage : null,
    onGenerate: generate,
    onGeneratePrompt: generatePrompt,
    onReview: () => reviewVideo(),
    onReviewVideo: reviewVideo,
    onUploadImage: () => fileInputRef.current?.click(),
    onUseProjectImage,
    onRefreshGallery: refreshGallery,
    canGenerate,
    canReview,
    canMakeNewVideo: stateScopeMatches && canManageProject && generationAvailable,
    canGeneratePrompt: Boolean(
      stateScopeMatches && canManageProject && projectId && !promptGenerating
    ),
    fileInputRef,
    onImageFileChange,
  };
}
