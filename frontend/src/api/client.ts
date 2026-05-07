import type {
  AttemptsResult,
  EventsResult,
  FilePreview,
  IssueDetail,
  StateSnapshot,
  WorkflowResource,
  WorkflowPutResult,
  WorkspaceListing,
} from "./types";

// In dev, Vite proxies /api → :7957. In prod, FastAPI serves both itself.
const API_BASE = "/api/v1";

let API_KEY: string | null = null;

export function setApiKey(key: string | null) {
  API_KEY = key && key.length ? key : null;
  if (API_KEY) localStorage.setItem("symphony.api_key", API_KEY);
  else localStorage.removeItem("symphony.api_key");
}

export function loadStoredApiKey() {
  const v = localStorage.getItem("symphony.api_key");
  if (v) API_KEY = v;
}

function authHeaders(): HeadersInit {
  return API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {};
}

async function jsonOrThrow<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let message = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) message = `${message}: ${body.detail}`;
      else if (body?.error) message = `${message}: ${body.error}`;
    } catch {
      /* swallow */
    }
    throw new Error(message);
  }
  return (await r.json()) as T;
}

export const api = {
  async state(): Promise<StateSnapshot> {
    return jsonOrThrow(
      await fetch(`${API_BASE}/state`, { headers: { ...authHeaders() } }),
    );
  },

  async issue(id: string): Promise<IssueDetail> {
    return jsonOrThrow(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}`, {
        headers: { ...authHeaders() },
      }),
    );
  },

  async issueAttempts(id: string): Promise<AttemptsResult> {
    return jsonOrThrow(
      await fetch(
        `${API_BASE}/issues/${encodeURIComponent(id)}/attempts`,
        { headers: { ...authHeaders() } },
      ),
    );
  },

  async issueEvents(
    id: string,
    opts: { attempt_number?: number; limit?: number } = {},
  ): Promise<EventsResult> {
    const q = new URLSearchParams();
    if (opts.attempt_number !== undefined)
      q.set("attempt_number", String(opts.attempt_number));
    if (opts.limit !== undefined) q.set("limit", String(opts.limit));
    return jsonOrThrow(
      await fetch(
        `${API_BASE}/issues/${encodeURIComponent(id)}/events?${q}`,
        { headers: { ...authHeaders() } },
      ),
    );
  },

  async workspace(id: string): Promise<WorkspaceListing> {
    return jsonOrThrow(
      await fetch(
        `${API_BASE}/issues/${encodeURIComponent(id)}/workspace`,
        { headers: { ...authHeaders() } },
      ),
    );
  },

  async workspaceFile(id: string, path: string): Promise<FilePreview> {
    const q = new URLSearchParams({ path });
    return jsonOrThrow(
      await fetch(
        `${API_BASE}/issues/${encodeURIComponent(id)}/workspace/file?${q}`,
        { headers: { ...authHeaders() } },
      ),
    );
  },

  async addHint(id: string, author: string, content: string) {
    return jsonOrThrow<{ id: number; consumed: boolean }>(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}/hint`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...authHeaders(),
        },
        body: JSON.stringify({ author, content }),
      }),
    );
  },

  async pause(id: string, reason?: string) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}/pause`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({ reason: reason ?? null }),
      }),
    );
  },
  async resume(id: string) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}/resume`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({}),
      }),
    );
  },
  async abort(id: string, message?: string) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}/abort`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message: message ?? null }),
      }),
    );
  },
  async retry(id: string) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/issues/${encodeURIComponent(id)}/retry`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({}),
      }),
    );
  },

  async emergencyStop(message?: string) {
    return jsonOrThrow<{
      ok: true;
      aborted_count: number;
      aborted_ids: string[];
    }>(
      await fetch(`${API_BASE}/emergency_stop`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message: message ?? null }),
      }),
    );
  },

  async reorderPending(orderedIssueIds: string[]) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/priority`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          ordered_issue_ids: orderedIssueIds,
          set_by: "dashboard",
          ttl_hours: 24,
        }),
      }),
    );
  },

  async patchConfig(patch: {
    max_concurrent_agents?: number;
    polling_interval_ms?: number;
  }) {
    return jsonOrThrow(
      await fetch(`${API_BASE}/config`, {
        method: "PATCH",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify(patch),
      }),
    );
  },

  async workflow(): Promise<WorkflowResource> {
    return jsonOrThrow(
      await fetch(`${API_BASE}/workflow`, { headers: { ...authHeaders() } }),
    );
  },

  async putWorkflow(content: string): Promise<WorkflowPutResult> {
    const r = await fetch(`${API_BASE}/workflow`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...authHeaders() },
      body: JSON.stringify({ content }),
    });
    // 422 returns a structured failure body with kept_previous=true.
    if (r.status === 422) {
      return (await r.json()) as WorkflowPutResult;
    }
    return jsonOrThrow<WorkflowPutResult>(r);
  },
};
