import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "./store";
import * as clientModule from "./api/client";
import { dashboardSocket } from "./api/ws";

// Force `init()` not to actually open a WebSocket; we'll drive the WS message
// pump manually via the mocked `on()` listener registry.
type WsListener = (m: unknown) => void;
let wsListeners: WsListener[] = [];

vi.mock("./api/ws", () => ({
  dashboardSocket: {
    on: vi.fn((fn: WsListener) => {
      wsListeners.push(fn);
      return () => {
        wsListeners = wsListeners.filter((l) => l !== fn);
      };
    }),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

vi.mock("./api/client", async () => {
  const actual = await vi.importActual<typeof import("./api/client")>(
    "./api/client",
  );
  return {
    ...actual,
    api: {
      state: vi.fn(),
      issue: vi.fn(),
      issueAttempts: vi.fn(),
      issueEvents: vi.fn(),
      workspace: vi.fn(),
      workspaceFile: vi.fn(),
      addHint: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
      abort: vi.fn(),
      retry: vi.fn(),
      reorderPending: vi.fn(),
      patchConfig: vi.fn(),
      workflow: vi.fn(),
      putWorkflow: vi.fn(),
    },
  };
});

const mockedApi = clientModule.api as unknown as Record<
  string,
  ReturnType<typeof vi.fn>
>;

function pushWS(msg: unknown) {
  for (const l of wsListeners) l(msg);
}

function resetStore() {
  useStore.setState({
    status: "closed",
    snapshot: null,
    recentEvents: {},
    activity: [],
    selectedIssueId: null,
    workflowEditorOpen: false,
    filters: { text: "", agent: null, priority: null, label: null },
    notice: null,
  });
}

describe("store", () => {
  beforeEach(() => {
    resetStore();
    wsListeners = [];
    Object.values(mockedApi).forEach((fn) => fn.mockReset?.());
    vi.useRealTimers();
  });

  describe("agent_event WS message", () => {
    it("appends to recentEvents and prepends activity", () => {
      vi.useFakeTimers();
      mockedApi.state.mockResolvedValue({
        tick_at: "",
        config: {},
        columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
        totals: { active_workers: 0, released_today: 0 },
      });
      useStore.getState().init();

      pushWS({
        type: "agent_event",
        issue_id: "id-MT-1",
        event: {
          kind: "tool_call",
          timestamp: "2026-05-06T03:14:00Z",
          data: { tool: "Bash" },
        },
      });

      const s = useStore.getState();
      expect(s.recentEvents["id-MT-1"]).toHaveLength(1);
      expect(s.activity).toHaveLength(1);
      expect(s.activity[0].issue_id).toBe("id-MT-1");
      expect(s.activity[0].summary).toContain("Bash");
    });

    it("does not push to activity when replay flag is set", () => {
      mockedApi.state.mockResolvedValue({
        tick_at: "",
        config: {},
        columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
        totals: { active_workers: 0, released_today: 0 },
      });
      useStore.getState().init();

      pushWS({
        type: "agent_event",
        issue_id: "id-X",
        replay: true,
        event: {
          kind: "message_delta",
          timestamp: "2026-05-06T03:14:00Z",
          data: { text: "hi" },
        },
      });

      const s = useStore.getState();
      expect(s.recentEvents["id-X"]).toHaveLength(1);
      expect(s.activity).toHaveLength(0);
    });
  });

  describe("state_snapshot / config_changed / workflow_reloaded", () => {
    it("init() does not fetch /state — WS state_snapshot replaces polling", () => {
      mockedApi.state.mockResolvedValue({
        tick_at: "",
        config: {},
        columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
        totals: { active_workers: 0, released_today: 0 },
      });
      useStore.getState().init();
      // Without state_snapshot from WS, snapshot stays null and no REST
      // /state call has been issued. Polling is gone in favour of WS.
      expect(mockedApi.state).toHaveBeenCalledTimes(0);
      expect(useStore.getState().snapshot).toBeNull();
    });

    it("state_snapshot WS message overwrites snapshot in place", () => {
      useStore.getState().init();
      const snapshot = {
        tick_at: "2026-05-15T00:00:00Z",
        config: { runner_kind: "echo" },
        columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
        totals: { active_workers: 0, released_today: 0 },
      };
      pushWS({ type: "state_snapshot", snapshot });

      const s = useStore.getState();
      expect(s.snapshot).toEqual(snapshot);
      // No REST call was triggered — pure WS reducer.
      expect(mockedApi.state).toHaveBeenCalledTimes(0);
    });

    it("fsm_transition no longer triggers a REST refresh", async () => {
      useStore.getState().init();
      mockedApi.state.mockClear();
      pushWS({ type: "fsm_transition", issue_id: "id-A", from: "claimed", to: "running" });
      await Promise.resolve();
      expect(mockedApi.state).toHaveBeenCalledTimes(0);
    });

    it("config_changed and workflow_reloaded set an info notice", () => {
      useStore.getState().init();

      pushWS({ type: "config_changed", config: {} });
      expect(useStore.getState().notice?.text).toMatch(/config/i);

      pushWS({ type: "workflow_reloaded", ok: true, config: {} });
      expect(useStore.getState().notice?.text).toMatch(/workflow/i);
    });
  });

  describe("filters", () => {
    it("setFilter merges patch into filters", () => {
      useStore.getState().setFilter({ text: "MT-1" });
      expect(useStore.getState().filters.text).toBe("MT-1");
      useStore.getState().setFilter({ priority: 2 });
      expect(useStore.getState().filters.priority).toBe(2);
      // text still kept
      expect(useStore.getState().filters.text).toBe("MT-1");
    });
  });

  describe("selectIssue / toggleWorkflowEditor / notice", () => {
    it("selectIssue sets the selected id", () => {
      useStore.getState().selectIssue("id-X");
      expect(useStore.getState().selectedIssueId).toBe("id-X");
      useStore.getState().selectIssue(null);
      expect(useStore.getState().selectedIssueId).toBeNull();
    });

    it("toggleWorkflowEditor flips when called without arg", () => {
      const before = useStore.getState().workflowEditorOpen;
      useStore.getState().toggleWorkflowEditor();
      expect(useStore.getState().workflowEditorOpen).toBe(!before);
    });

    it("clearNotice removes notice", () => {
      useStore.getState().setNotice({ kind: "info", text: "hello" });
      expect(useStore.getState().notice).not.toBeNull();
      useStore.getState().clearNotice();
      expect(useStore.getState().notice).toBeNull();
    });
  });

  describe("ring buffer cap on recentEvents", () => {
    it("caps per-issue events at 200", () => {
      mockedApi.state.mockResolvedValue({
        tick_at: "",
        config: {},
        columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
        totals: { active_workers: 0, released_today: 0 },
      });
      useStore.getState().init();

      for (let i = 0; i < 250; i++) {
        pushWS({
          type: "agent_event",
          issue_id: "id-Z",
          event: {
            kind: "message_delta",
            timestamp: `2026-05-06T03:14:${String(i % 60).padStart(2, "0")}Z`,
            data: { text: `t${i}` },
          },
        });
      }

      const events = useStore.getState().recentEvents["id-Z"];
      expect(events.length).toBe(200);
    });
  });
});

// Self-test the ws mock import wires together (sanity)
describe("ws mock wiring", () => {
  it("dashboardSocket.on is the mock", () => {
    expect(vi.isMockFunction(dashboardSocket.on)).toBe(true);
  });
});
