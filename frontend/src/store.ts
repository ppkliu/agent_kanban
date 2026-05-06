import { create } from "zustand";
import { api } from "./api/client";
import { dashboardSocket } from "./api/ws";
import type { EventRecord, StateSnapshot, WSMessage } from "./api/types";

interface Filters {
  text: string;
  agent: string | null;
  priority: number | null;
  label: string | null;
}

interface ActivityItem {
  id: number; // monotonic counter for React keys
  issue_id: string;
  kind: string;
  timestamp: string;
  summary: string;
}

interface Store {
  status: "connecting" | "open" | "closed";
  snapshot: StateSnapshot | null;
  // Recent live events per issue (capped — full history via REST).
  recentEvents: Record<string, EventRecord[]>;
  activity: ActivityItem[];
  selectedIssueId: string | null;
  workflowEditorOpen: boolean;
  filters: Filters;
  notice: { kind: "info" | "error"; text: string } | null;

  // ---- Actions ----
  init: () => void;
  refresh: () => Promise<void>;
  selectIssue: (id: string | null) => void;
  toggleWorkflowEditor: (open?: boolean) => void;
  setFilter: (patch: Partial<Filters>) => void;
  clearNotice: () => void;
  setNotice: (n: { kind: "info" | "error"; text: string } | null) => void;
}

let activityCounter = 0;
const RECENT_PER_ISSUE = 200;
const ACTIVITY_CAP = 80;

function summarizeEvent(kind: string, data: Record<string, unknown>): string {
  switch (kind) {
    case "tool_call":
      return `→ tool_call: ${(data as { tool?: string }).tool ?? "?"}`;
    case "tool_result":
      return `→ tool_result${(data as { is_error?: boolean }).is_error ? " (error)" : ""}`;
    case "message_delta": {
      const text = (data as { text?: string }).text;
      if (!text) return "→ message";
      return `→ "${text.slice(0, 60)}${text.length > 60 ? "…" : ""}"`;
    }
    case "turn_started":
      return "→ turn started";
    case "turn_completed": {
      const cost = (data as { cost_usd?: number }).cost_usd;
      return `→ turn completed${cost != null ? ` $${cost.toFixed(4)}` : ""}`;
    }
    case "error":
      return `⚠ ${(data as { message?: string }).message ?? "error"}`;
    case "done":
      return `✓ done`;
    default:
      return `→ ${kind}`;
  }
}

export const useStore = create<Store>((set, get) => ({
  status: "closed",
  snapshot: null,
  recentEvents: {},
  activity: [],
  selectedIssueId: null,
  workflowEditorOpen: false,
  filters: { text: "", agent: null, priority: null, label: null },
  notice: null,

  init: () => {
    dashboardSocket.onStatus((s) => set({ status: s }));
    dashboardSocket.on((m: WSMessage) => {
      if (m.type === "agent_event") {
        const ev: EventRecord = {
          id: 0,
          attempt_number: 0,
          kind: m.event.kind,
          timestamp: m.event.timestamp,
          data: m.event.data,
        };
        set((s) => {
          const cur = s.recentEvents[m.issue_id] ?? [];
          const next = [...cur, ev];
          if (next.length > RECENT_PER_ISSUE) next.shift();
          const activity: ActivityItem = {
            id: ++activityCounter,
            issue_id: m.issue_id,
            kind: ev.kind,
            timestamp: ev.timestamp,
            summary: summarizeEvent(ev.kind, ev.data),
          };
          const newActivity = m.replay
            ? s.activity
            : [activity, ...s.activity].slice(0, ACTIVITY_CAP);
          return {
            recentEvents: { ...s.recentEvents, [m.issue_id]: next },
            activity: newActivity,
          };
        });
      } else if (m.type === "fsm_transition") {
        // Optimistic refresh — the next /state poll will reconcile fully.
        get().refresh();
      } else if (m.type === "config_changed") {
        get().refresh();
        set({
          notice: { kind: "info", text: "Config reloaded" },
        });
      } else if (m.type === "workflow_reloaded") {
        get().refresh();
        set({
          notice: { kind: "info", text: "WORKFLOW.md reloaded" },
        });
      }
    });
    dashboardSocket.connect();
    void get().refresh();
    // Periodic snapshot poll as a safety net for missed transitions.
    setInterval(() => {
      void get().refresh();
    }, 5000);
  },

  refresh: async () => {
    try {
      const snap = await api.state();
      set({ snapshot: snap });
    } catch (e) {
      set({
        notice: {
          kind: "error",
          text: `state fetch failed: ${(e as Error).message}`,
        },
      });
    }
  },

  selectIssue: (id) => set({ selectedIssueId: id }),

  toggleWorkflowEditor: (open) =>
    set((s) => ({
      workflowEditorOpen: open ?? !s.workflowEditorOpen,
    })),

  setFilter: (patch) =>
    set((s) => ({ filters: { ...s.filters, ...patch } })),

  clearNotice: () => set({ notice: null }),
  setNotice: (n) => set({ notice: n }),
}));
