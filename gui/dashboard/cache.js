// Client-side read cache for the dashboard's read-only GET requests.
//
// The cache reduces redundant HTTP traffic from browser dashboard clients to
// the local MCP server while preserving fresh results after writes and explicit
// refreshes. It is deliberately narrow: it only ever holds successful GET
// payloads keyed by normalized path, never credentials, mutation responses, or
// failed responses. See docs/mcp-server/dashboard.md for the freshness and
// privacy semantics this implements.
//
// Three selectable modes:
//   - "off":          no caching; every read hits the network.
//   - "memory":       in-process Map, cleared on reload.
//   - "localStorage": survives reload, falls back to memory when storage is
//                     unavailable, quota-limited, or corrupt.
//
// TTL policy is centralized (CACHE_POLICY): volatile dashboard/todo/task reads
// use a short TTL, stable documentation detail/scope reads use a longer TTL so
// endpoints never implement caching independently.

export const CACHE_MODES = ["off", "memory", "localStorage"];
export const DEFAULT_CACHE_MODE = "memory";

// Schema version for persisted entries. Bumping it discards every previously
// stored entry instead of risking a shape mismatch on read.
export const CACHE_SCHEMA_VERSION = 1;
export const CACHE_STORAGE_PREFIX = "mcpDashboard.readCache.v1";

// Centralized TTL policy in milliseconds. Volatile reads refresh quickly;
// stable documentation reads can live much longer.
export const SHORT_TTL_MS = 10_000;
export const LONG_TTL_MS = 300_000;

// Bounded number of live entries per mode; oldest-by-fetched_at is evicted
// first so a long session cannot grow the cache without limit.
export const CACHE_MAX_ENTRIES = 64;

// Route family -> TTL. Matched against the pathname portion of a normalized key
// (query string stripped). The first matching entry wins, so order longest /
// most-specific first. `exact: true` requires the path to equal the prefix
// (its query string, if any, is what varies) so a list-read prefix never also
// matches its POST mutation sub-paths (e.g. /api/dashboard/todos/prune).
const CACHE_POLICY = [
  { prefix: "/docs/doc", ttl: LONG_TTL_MS },
  { prefix: "/docs/scope/", ttl: LONG_TTL_MS },
  { prefix: "/api/dashboard/todos/groups/", ttl: SHORT_TTL_MS },
  { prefix: "/api/dashboard/todos", ttl: SHORT_TTL_MS, exact: true },
  { prefix: "/api/dashboard", ttl: SHORT_TTL_MS, exact: true },
];

/** TTL for a normalized cache key; null when the route is not cacheable. */
export function ttlForKey(key) {
  const path = String(key || "").split("?", 1)[0];
  for (const { prefix, ttl, exact } of CACHE_POLICY) {
    if (exact ? path === prefix : path.startsWith(prefix)) return ttl;
  }
  return null;
}

/** Whether a normalized key is eligible for read caching at all. */
export function isCacheableKey(key) {
  return ttlForKey(key) !== null;
}

// Query-parameter normalization: sort keys so `?a=1&b=2` and `?b=2&a=1` share a
// cache entry. The base path plus a stable query string forms the key.
export function normalizeCacheKey(pathname) {
  const raw = String(pathname || "");
  const queryIndex = raw.indexOf("?");
  if (queryIndex < 0) return raw;
  const base = raw.slice(0, queryIndex);
  const params = new URLSearchParams(raw.slice(queryIndex + 1));
  params.sort();
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function normalizeMode(mode) {
  return CACHE_MODES.includes(mode) ? mode : DEFAULT_CACHE_MODE;
}

export class DashboardCache {
  constructor({ mode = DEFAULT_CACHE_MODE, storage = globalThis.window?.localStorage, now } = {}) {
    // Coalesced in-flight reads: key -> Promise. This is mode-independent so an
    // "off" cache still dedupes identical concurrent reads.
    this.inflight = new Map();
    // In-memory entries: key -> { fetched_at, payload }. Also the fallback
    // store whenever localStorage is unavailable.
    this.memory = new Map();
    this.storage = storage;
    // Set to true once localStorage has failed, so we stop retrying it and use
    // memory for the rest of the session.
    this.storageBroken = false;
    // Injectable clock keeps expiry deterministic under test.
    this.now = typeof now === "function" ? now : () => Date.now();
    this.setMode(mode);
  }

  setMode(mode) {
    const next = normalizeMode(mode);
    // Switching modes drops any entries that belonged to the previous mode so
    // stale cross-mode reads cannot leak; in-flight coalescing is preserved.
    if (next !== this.mode) {
      this.memory.clear();
      if (next !== "localStorage") this.clearStorage();
    }
    this.mode = next;
    if (this.mode === "localStorage") this.pruneStorageSchema();
    return this.mode;
  }

  storageKey(key) {
    return `${CACHE_STORAGE_PREFIX}::${key}`;
  }

  // --- localStorage helpers (all failure-tolerant) --------------------------

  usableStorage() {
    return this.mode === "localStorage" && this.storage && !this.storageBroken ? this.storage : null;
  }

  markStorageBroken() {
    // First failure downgrades to memory for the session without breaking the
    // dashboard; entries already in this.memory stay usable.
    this.storageBroken = true;
  }

  readStorageEntry(key) {
    const storage = this.usableStorage();
    if (!storage) return null;
    try {
      const raw = storage.getItem(this.storageKey(key));
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== CACHE_SCHEMA_VERSION ||
          typeof parsed.fetched_at !== "number" || !("payload" in parsed)) {
        // Corrupt or wrong-schema blob: drop it and treat as a miss.
        this.removeStorageEntry(key);
        return null;
      }
      return { fetched_at: parsed.fetched_at, payload: parsed.payload };
    } catch {
      // Corrupt JSON or a throwing getItem: fall back to a miss.
      this.markStorageBroken();
      return null;
    }
  }

  writeStorageEntry(key, entry) {
    const storage = this.usableStorage();
    if (!storage) return false;
    try {
      storage.setItem(this.storageKey(key), JSON.stringify({
        version: CACHE_SCHEMA_VERSION,
        fetched_at: entry.fetched_at,
        payload: entry.payload,
      }));
      return true;
    } catch {
      // Quota exceeded or storage disabled mid-session: evict once and retry;
      // if it still fails, downgrade to memory permanently.
      if (this.evictStorageOldest()) {
        try {
          storage.setItem(this.storageKey(key), JSON.stringify({
            version: CACHE_SCHEMA_VERSION,
            fetched_at: entry.fetched_at,
            payload: entry.payload,
          }));
          return true;
        } catch {
          this.markStorageBroken();
          return false;
        }
      }
      this.markStorageBroken();
      return false;
    }
  }

  removeStorageEntry(key) {
    const storage = this.storage;
    if (!storage) return;
    try { storage.removeItem(this.storageKey(key)); } catch { /* ignore */ }
  }

  storageEntryKeys() {
    const storage = this.storage;
    if (!storage) return [];
    const keys = [];
    try {
      const total = storage.length ?? 0;
      for (let i = 0; i < total; i += 1) {
        const full = storage.key(i);
        if (typeof full === "string" && full.startsWith(`${CACHE_STORAGE_PREFIX}::`)) keys.push(full);
      }
    } catch {
      return [];
    }
    return keys;
  }

  clearStorage() {
    const storage = this.storage;
    if (!storage) return;
    for (const full of this.storageEntryKeys()) {
      try { storage.removeItem(full); } catch { /* ignore */ }
    }
  }

  // Drop any persisted entry whose blob does not parse under the current
  // schema version, so a version bump invalidates the on-disk cache.
  pruneStorageSchema() {
    const storage = this.usableStorage();
    if (!storage) return;
    for (const full of this.storageEntryKeys()) {
      try {
        const raw = storage.getItem(full);
        const parsed = raw ? JSON.parse(raw) : null;
        if (!parsed || parsed.version !== CACHE_SCHEMA_VERSION) storage.removeItem(full);
      } catch {
        try { storage.removeItem(full); } catch { /* ignore */ }
      }
    }
  }

  // Evict the oldest persisted entry (lowest fetched_at) to make room; returns
  // true when something was removed.
  evictStorageOldest() {
    const storage = this.storage;
    if (!storage) return false;
    let oldestFull = null;
    let oldestAt = Infinity;
    for (const full of this.storageEntryKeys()) {
      try {
        const parsed = JSON.parse(storage.getItem(full) || "null");
        const at = parsed && typeof parsed.fetched_at === "number" ? parsed.fetched_at : 0;
        if (at < oldestAt) { oldestAt = at; oldestFull = full; }
      } catch {
        oldestFull = full; oldestAt = 0; break;
      }
    }
    if (oldestFull) { try { storage.removeItem(oldestFull); return true; } catch { /* ignore */ } }
    return false;
  }

  // --- Entry read/write across modes ----------------------------------------

  entryFresh(entry, key) {
    if (!entry || typeof entry.fetched_at !== "number") return false;
    const ttl = ttlForKey(key);
    if (ttl === null) return false;
    return this.now() - entry.fetched_at < ttl;
  }

  getEntry(key) {
    if (this.mode === "off") return null;
    if (this.mode === "localStorage") {
      const stored = this.readStorageEntry(key);
      if (stored) return stored;
      // When storage broke mid-session we keep serving memory entries.
      return this.storageBroken ? this.memory.get(key) || null : null;
    }
    return this.memory.get(key) || null;
  }

  setEntry(key, payload) {
    if (this.mode === "off" || !isCacheableKey(key)) return;
    const entry = { fetched_at: this.now(), payload };
    if (this.mode === "localStorage") {
      const wrote = this.writeStorageEntry(key, entry);
      if (wrote) { this.evictMemoryIfNeeded(); return; }
      // Storage failed: fall through to memory so the read is still cached.
    }
    this.memory.set(key, entry);
    this.evictMemoryIfNeeded();
  }

  evictMemoryIfNeeded() {
    while (this.memory.size > CACHE_MAX_ENTRIES) {
      // Map iteration is insertion-ordered; the first key is the oldest write.
      const oldest = this.memory.keys().next().value;
      if (oldest === undefined) break;
      this.memory.delete(oldest);
    }
  }

  /** Fresh cached payload for `key`, or null on miss/expiry/off. */
  peek(key) {
    const normalized = normalizeCacheKey(key);
    const entry = this.getEntry(normalized);
    return this.entryFresh(entry, normalized) ? entry.payload : null;
  }

  /**
   * Read through the cache. `loader()` performs the network fetch and must
   * resolve to the parsed payload. `bypass` skips any fresh entry (manual
   * Refresh) but still writes the fresh result back and still coalesces
   * concurrent identical reads.
   */
  async read(key, loader, { bypass = false } = {}) {
    const normalized = normalizeCacheKey(key);
    const cacheable = isCacheableKey(normalized) && this.mode !== "off";

    if (cacheable && !bypass) {
      const entry = this.getEntry(normalized);
      if (this.entryFresh(entry, normalized)) return entry.payload;
    }

    // Coalesce identical in-flight reads regardless of mode, so a burst of
    // callers shares one network round-trip. A bypass read still joins an
    // in-flight bypass/normal read rather than issuing a duplicate request.
    const pending = this.inflight.get(normalized);
    if (pending) return pending;

    const promise = (async () => {
      const payload = await loader();
      if (cacheable) this.setEntry(normalized, payload);
      return payload;
    })();
    // Track the raw promise so followers share it; clear on settle so a later
    // read re-fetches once the entry is gone/expired.
    this.inflight.set(normalized, promise);
    try {
      return await promise;
    } finally {
      if (this.inflight.get(normalized) === promise) this.inflight.delete(normalized);
    }
  }

  /**
   * Invalidate every cached read whose normalized key starts with any of the
   * given path prefixes. Called after successful mutations so the next read
   * revalidates. In-flight reads are left alone: they already predate the
   * mutation only briefly and their result is discarded from cache on the next
   * invalidation if needed; callers refresh explicitly after mutating.
   */
  invalidatePrefixes(prefixes) {
    const list = (Array.isArray(prefixes) ? prefixes : [prefixes]).map(String).filter(Boolean);
    if (!list.length) return;
    const matches = (key) => list.some((prefix) => key === prefix || key.startsWith(prefix));

    for (const key of Array.from(this.memory.keys())) {
      if (matches(key)) this.memory.delete(key);
    }
    if (this.storage) {
      const strip = `${CACHE_STORAGE_PREFIX}::`;
      for (const full of this.storageEntryKeys()) {
        if (matches(full.slice(strip.length))) {
          try { this.storage.removeItem(full); } catch { /* ignore */ }
        }
      }
    }
  }

  /** Drop everything (mode change, hard reset). */
  clear() {
    this.memory.clear();
    this.clearStorage();
    // In-flight reads keep running; their write-back is a no-op once cleared.
  }
}

// Which cached read prefixes each mutation route invalidates. Todo mutations
// change both the bounded dashboard payload (open_todos) and the scoped todo
// list plus any affected group; documentation-reference appends also touch
// todos and the underlying doc detail; everything else is reflected in the
// dashboard snapshot. Keys are matched by exact route path.
export const MUTATION_INVALIDATIONS = {
  "/api/dashboard/todos/priority": ["/api/dashboard", "/api/dashboard/todos"],
  "/api/dashboard/todos/scope": ["/api/dashboard", "/api/dashboard/todos"],
  "/api/dashboard/todos/prune": ["/api/dashboard", "/api/dashboard/todos"],
  "/api/dashboard/todos/references": ["/api/dashboard", "/api/dashboard/todos", "/docs/doc"],
  "/api/dashboard/todos/groups/reorder": ["/api/dashboard", "/api/dashboard/todos", "/api/dashboard/todos/groups/"],
  "/api/dashboard/owner/flush-agent-state": ["/api/dashboard"],
  "/api/dashboard/recommendations/status": ["/api/dashboard"],
  "/api/dashboard/guidance-recommendations/add": ["/api/dashboard", "/api/dashboard/guidance-recommendations"],
  "/api/dashboard/guidance-recommendations/reconcile": ["/api/dashboard", "/api/dashboard/guidance-recommendations"],
  "/api/dashboard/token-usage/agent-category/rename": ["/api/dashboard"],
  "/api/dashboard/token-usage/agent-category/purge-closed": ["/api/dashboard"],
  "/api/dashboard/orchestration/enqueue": ["/api/dashboard"],
  "/api/dashboard/orchestration/wake": ["/api/dashboard"],
  "/api/dashboard/orchestration/retry": ["/api/dashboard"],
  "/api/dashboard/orchestration/drop": ["/api/dashboard"],
  "/api/dashboard/orchestration/stop": ["/api/dashboard"],
  "/api/dashboard/orchestration/cancel": ["/api/dashboard"],
};

/** Prefixes a mutation route should invalidate, or [] when it caches nothing. */
export function invalidationsForPath(pathname) {
  const path = String(pathname || "").split("?", 1)[0];
  return MUTATION_INVALIDATIONS[path] || [];
}
