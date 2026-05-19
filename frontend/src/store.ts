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
  // Phase F WS heartbeat: epoch-ms when the last server heartbeat arrived,
  // plus the orchestrator tick counter it reported. Consumers compare
  // `Date.now() - lastHeartbeatAt` to detect a stale WS. The interval the
  // server reports (`next_heartbeat_after_s`) feeds dynamic stale
  // thresholds so idle-mode (150s cadence) does not trip the warning that
  // is sized for active-mode (15s cadence).
  lastHeartbeatAt: number | null;
  lastOrchestratorTicks: number | null;
  lastIdle: boolean | null;
  lastHeartbeatIntervalS: number | null;

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

export const useStore = create<Store>((set) => ({
  status: "closed",
  snapshot: null,
  recentEvents: {},
  activity: [],
  selectedIssueId: null,
  workflowEditorOpen: false,
  filters: { text: "", agent: null, priority: null, label: null },
  notice: null,
  lastHeartbeatAt: null,
  lastOrchestratorTicks: null,
  lastIdle: null,
  lastHeartbeatIntervalS: null,

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
      } else if (m.type === "state_snapshot") {
        // Server pushes a fresh snapshot on WS connect + after every
        // state-mutating event (fsm_transition / config_changed /
        // workflow_reloaded). No REST polling needed.
        set({ snapshot: m.snapshot });
      } else if (m.type === "config_changed") {
        set({ notice: { kind: "info", text: "Config reloaded" } });
      } else if (m.type === "workflow_reloaded") {
        set({ notice: { kind: "info", text: "WORKFLOW.md reloaded" } });
      } else if (m.type === "heartbeat") {
        set({
          lastHeartbeatAt: Date.now(),
          lastOrchestratorTicks: m.orchestrator_ticks,
          lastIdle: m.idle,
          lastHeartbeatIntervalS: m.next_heartbeat_after_s,
        });
      }
      // fsm_transition is informational here — the server follows it with a
      // state_snapshot push that updates `snapshot` for us.
    });
    dashboardSocket.connect();
    // No initial REST fetch, no setInterval polling — the WS push of
    // state_snapshot on connect is the single source of truth. refresh()
    // remains exported as a manual escape hatch.
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
