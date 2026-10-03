/** Small, identity-scoped caches for the community gallery (never persisted). */
export function galleryPageKey(scope: string, pageSize: number, page: number, search: string): string {
  return JSON.stringify([scope, pageSize, Math.max(0, page), search.trim()]);
}

export class GalleryPageCache<T> {
  private readonly entries = new Map<string, { value: T; expiresAt: number }>();
  private readonly maxEntries: number;
  private readonly ttlMs: number;

  constructor(maxEntries = 12, ttlMs = 30_000) {
    this.maxEntries = Math.max(1, maxEntries);
    this.ttlMs = Math.max(0, ttlMs);
  }

  get(key: string, now = Date.now()): T | undefined {
    const entry = this.entries.get(key);
    return entry && entry.expiresAt > now ? entry.value : undefined;
  }

  set(key: string, value: T, now = Date.now()): void {
    this.entries.delete(key);
    this.entries.set(key, { value, expiresAt: now + this.ttlMs });
    while (this.entries.size > this.maxEntries) {
      this.entries.delete(this.entries.keys().next().value!);
    }
  }

  clear(): void {
    this.entries.clear();
  }
}

/** Retain pending siblings when one thumbnail settles; abort only obsolete work. */
export class GalleryImageRequests<T> {
  private scope = "";
  private readonly pending = new Map<string, AbortController>();

  sync(
    scope: string,
    ids: readonly string[],
    load: (id: string, signal: AbortSignal) => Promise<T>,
    onResult: (id: string, value: T) => void,
    onError: (id: string, error: unknown) => void,
  ): void {
    if (scope !== this.scope) {
      this.clear();
      this.scope = scope;
    }
    const wanted = new Set(ids);
    for (const [id, controller] of this.pending) {
      if (!wanted.has(id)) {
        controller.abort();
        this.pending.delete(id);
      }
    }
    for (const id of wanted) {
      if (this.pending.has(id)) continue;
      const controller = new AbortController();
      this.pending.set(id, controller);
      const current = () => !controller.signal.aborted && this.pending.get(id) === controller;
      void (async () => {
        try {
          const value = await load(id, controller.signal);
          if (current()) onResult(id, value);
        } catch (error) {
          if (current()) onError(id, error);
        } finally {
          if (this.pending.get(id) === controller) this.pending.delete(id);
        }
      })();
    }
  }

  clear(): void {
    for (const controller of this.pending.values()) controller.abort();
    this.pending.clear();
  }
}
