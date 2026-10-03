import type { FormaProjectListResponse, FormaProjectResponse, FormaProjectSummary } from "./contracts";

export type FormaApiRequest = {
  path: string;
  method: string;
};

export type FormaApiClientOptions = {
  /** API origin or API root. A missing `/api` suffix is added automatically. */
  baseUrl?: string;
  getHeaders?: (request: FormaApiRequest) => HeadersInit | Promise<HeadersInit>;
  fetcher?: typeof fetch;
};

export class FormaApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly correlationId: string | null;

  constructor(message: string, status: number, code: string | null = null, correlationId: string | null = null) {
    super(message);
    this.name = "FormaApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }

  get unauthorized() {
    return this.status === 401 || this.status === 403;
  }
}

function normalizeBaseUrl(value: string | undefined) {
  const trimmed = (value || "/api").trim().replace(/\/+$/, "");
  if (!trimmed) return "/api";
  return trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function statusMessage(status: number) {
  if (status === 401 || status === 403) return "Authorization is required to view this project.";
  if (status === 404) return "The requested Forma project was not found.";
  if (status === 429) return "Forma is temporarily rate limited. Try again shortly.";
  if (status >= 500) return "Forma could not complete that request.";
  return "Forma returned an unexpected response.";
}

function errorFields(body: unknown) {
  const root = asRecord(body);
  const detail = root ? root.detail ?? root.error ?? root : body;
  const record = asRecord(detail);
  const code = asString(record?.code) || asString(root?.code);
  const correlationId = asString(record?.correlation_id) || asString(root?.correlation_id);
  return {
    code,
    correlationId,
    message: code || correlationId
      ? asString(record?.message) || asString(record?.detail) || asString(root?.message)
      : null,
  };
}

export class FormaApiClient {
  readonly baseUrl: string;
  private readonly getHeaders?: FormaApiClientOptions["getHeaders"];
  private readonly fetcher: typeof fetch;

  constructor(options: FormaApiClientOptions = {}) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    this.getHeaders = options.getHeaders;
    const fetcher = options.fetcher || (typeof fetch === "function" ? fetch.bind(globalThis) : null);
    if (!fetcher) throw new Error("FormaApiClient requires a fetch implementation in this environment.");
    this.fetcher = fetcher;
  }

  async listProjects(options: {
    scope?: "community" | "mine";
    search?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<FormaProjectListResponse> {
    const params = new URLSearchParams();
    if (options.search?.trim()) params.set("q", options.search.trim());
    if (options.limit !== undefined) params.set("limit", String(Math.max(1, Math.floor(options.limit))));
    if (options.offset !== undefined) params.set("offset", String(Math.max(0, Math.floor(options.offset))));
    const path = options.scope === "mine" ? "/my/projects" : "/projects";
    const response = await this.request<FormaProjectListResponse | FormaProjectSummary[]>(`${path}?${params.toString()}`);
    if (Array.isArray(response)) {
      return { items: response, total: response.length };
    }
    return {
      items: Array.isArray(response?.items) ? response.items : [],
      total: Number.isFinite(Number(response?.total)) ? Math.max(0, Number(response.total)) : 0,
      limit: response?.limit,
      offset: response?.offset,
    };
  }

  getProject(projectId: string) {
    return this.request<FormaProjectResponse>(`/projects/${encodeURIComponent(projectId)}`);
  }

  getImageSummary(projectId: string) {
    return this.request<Record<string, unknown>>(`/projects/${encodeURIComponent(projectId)}/image-summary`);
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const method = (init.method || "GET").toUpperCase();
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const configuredHeaders = await this.getHeaders?.({ path, method });
    if (configuredHeaders) {
      new Headers(configuredHeaders).forEach((value, key) => headers.set(key, value));
    }

    let response: Response;
    try {
      response = await this.fetcher(`${this.baseUrl}${path}`, { ...init, headers });
    } catch {
      throw new FormaApiError("Forma is unavailable. Check the API URL and try again.", 0, "api_unavailable");
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Empty responses are valid for some API operations.
    }
    if (!response.ok) {
      const fields = errorFields(body);
      throw new FormaApiError(fields.message || statusMessage(response.status), response.status, fields.code, fields.correlationId);
    }
    return body as T;
  }
}

export type { FormaProjectListResponse, FormaProjectResponse, FormaProjectSummary } from "./contracts";
