import test from "node:test";
import assert from "node:assert/strict";

import { DashboardApi } from "./api.js";
import {
  DashboardCache,
  normalizeCacheKey,
  ttlForKey,
  invalidationsForPath,
  SHORT_TTL_MS,
  LONG_TTL_MS,
  CACHE_MAX_ENTRIES,
} from "./cache.js";

// A minimal in-memory localStorage stand-in whose failure modes are injectable.
function fakeStorage({ failSet = false } = {}) {
  const map = new Map();
  return {
    map,
    failSet,
    get length() { return map.size; },
    key(i) { return Array.from(map.keys())[i] ?? null; },
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { if (this.failSet) throw new Error("quota exceeded"); map.set(k, String(v)); },
    removeItem(k) { map.delete(k); },
  };
}

// Counting fetch stub that returns a distinct JSON payload per call.
function countingApi({ mode = "memory", storage, now } = {}) {
  const calls = [];
  const cache = new DashboardCache({ mode, storage, now });
  const api = new DashboardApi({
    basePathOverride: "",
    cache,
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return {
        ok: true,
        json: async () => ({ path, n: calls.length }),
      };
    },
  });
  return { api, cache, calls };
}

test("cache key normalization sorts query params and TTL policy splits volatile vs stable", () => {
  assert.equal(normalizeCacheKey("/api/dashboard/todos?b=2&a=1"), "/api/dashboard/todos?a=1&b=2");
  assert.equal(normalizeCacheKey("/api/dashboard"), "/api/dashboard");
  assert.equal(ttlForKey("/api/dashboard"), SHORT_TTL_MS);
  assert.equal(ttlForKey("/api/dashboard/todos?a=1"), SHORT_TTL_MS);
  assert.equal(ttlForKey("/docs/doc?key=x"), LONG_TTL_MS);
  assert.equal(ttlForKey("/docs/scope/mcp-server"), LONG_TTL_MS);
  // Non-read routes are not cacheable.
  assert.equal(ttlForKey("/api/dashboard/todos/prune"), null);
});

test("read cache serves a hit within TTL and refetches after expiry", async () => {
  let clock = 1000;
  const { api, calls } = countingApi({ mode: "memory", now: () => clock });
  const first = await api.dashboard();
  const second = await api.dashboard();
  assert.deepEqual(first, second);
  assert.equal(calls.length, 1, "second read served from cache");
  clock += SHORT_TTL_MS + 1;
  await api.dashboard();
  assert.equal(calls.length, 2, "expired entry refetched");
});

test("in-flight identical reads are coalesced into one request", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const cache = new DashboardCache({ mode: "memory" });
  const api = new DashboardApi({
    basePathOverride: "",
    cache,
    fetchImpl: async (path) => {
      calls.push(path);
      await gate;
      return { ok: true, json: async () => ({ path }) };
    },
  });
  const p1 = api.dashboard();
  const p2 = api.dashboard();
  release();
  const [a, b] = await Promise.all([p1, p2]);
  assert.deepEqual(a, b);
  assert.equal(calls.length, 1, "concurrent reads shared one fetch");
});

test("manual bypass revalidates past a fresh entry", async () => {
  const { api, calls } = countingApi({ mode: "memory" });
  await api.dashboard();
  assert.equal(calls.length, 1);
  await api.dashboard();
  assert.equal(calls.length, 1, "fresh entry served without bypass");
  await api.dashboard({ bypass: true });
  assert.equal(calls.length, 2, "bypass forced a refetch");
});

test("off mode never caches", async () => {
  const { api, calls } = countingApi({ mode: "off" });
  await api.dashboard();
  await api.dashboard();
  assert.equal(calls.length, 2, "off mode refetches every read");
});

test("successful mutation invalidates affected cached reads", async () => {
  const { api, calls } = countingApi({ mode: "memory" });
  await api.dashboard();
  await api.todos();
  assert.equal(calls.length, 2);
  // Cached.
  await api.dashboard();
  await api.todos();
  assert.equal(calls.length, 2);
  // A prune invalidates both /api/dashboard and /api/dashboard/todos.
  await api.pruneTodo({ todo_key: "t", status: "done" });
  assert.equal(calls.length, 3, "mutation POST is a real request");
  await api.dashboard();
  await api.todos();
  assert.equal(calls.length, 5, "both reads revalidated after invalidation");
});

test("mutation invalidation map covers todo, doc-ref, and dashboard-wide routes", () => {
  assert.deepEqual(invalidationsForPath("/api/dashboard/todos/prune"), ["/api/dashboard", "/api/dashboard/todos"]);
  assert.ok(invalidationsForPath("/api/dashboard/todos/references").includes("/docs/doc"));
  assert.deepEqual(invalidationsForPath("/api/dashboard/recommendations/status"), ["/api/dashboard"]);
  assert.deepEqual(invalidationsForPath("/docs/search"), []);
});

test("mutation responses are never cached", async () => {
  const { api, calls } = countingApi({ mode: "memory" });
  await api.pruneTodo({ todo_key: "t", status: "done" });
  await api.pruneTodo({ todo_key: "t", status: "done" });
  assert.equal(calls.length, 2, "each mutation hits the network");
});

test("localStorage mode persists entries across cache instances", async () => {
  const storage = fakeStorage();
  const first = countingApi({ mode: "localStorage", storage });
  await first.api.dashboard();
  assert.equal(first.calls.length, 1);
  // A fresh cache over the same storage reads the persisted entry.
  const second = countingApi({ mode: "localStorage", storage });
  const payload = await second.api.dashboard();
  assert.equal(second.calls.length, 0, "persisted entry served without a fetch");
  assert.equal(payload.path, "/api/dashboard");
});

test("localStorage failure falls back to memory without breaking reads", async () => {
  const storage = fakeStorage({ failSet: true });
  const { api, cache, calls } = countingApi({ mode: "localStorage", storage });
  const a = await api.dashboard();
  const b = await api.dashboard();
  assert.deepEqual(a, b);
  assert.equal(calls.length, 1, "read still cached in memory after storage set failed");
  assert.equal(cache.storageBroken, true);
});

test("corrupt localStorage blob is treated as a miss, not a crash", async () => {
  const storage = fakeStorage();
  storage.setItem("mcpDashboard.readCache.v1::/api/dashboard", "{not valid json");
  const { api, calls } = countingApi({ mode: "localStorage", storage });
  await api.dashboard();
  assert.equal(calls.length, 1, "corrupt entry ignored and refetched");
});

test("bounded eviction keeps the cache under the entry cap", async () => {
  const cache = new DashboardCache({ mode: "memory" });
  for (let i = 0; i < CACHE_MAX_ENTRIES + 10; i += 1) {
    cache.setEntry(`/api/dashboard/todos?scope=${i}`, { i });
  }
  assert.ok(cache.memory.size <= CACHE_MAX_ENTRIES, `cache bounded to ${CACHE_MAX_ENTRIES}`);
});

test("DashboardApi binds the browser fetch receiver", async () => {
  const originalFetch = globalThis.fetch;
  let receiver = null;
  globalThis.fetch = function fetchStub() {
    receiver = this;
    return Promise.resolve(new Response(JSON.stringify({ project: "smoke" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  };

  try {
    const { DashboardApi } = await import(`./api.js?fetch-binding=${Date.now()}`);
    const payload = await new DashboardApi({ basePathOverride: "" }).dashboard();
    assert.deepEqual(payload, { project: "smoke" });
    assert.equal(receiver, globalThis);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
