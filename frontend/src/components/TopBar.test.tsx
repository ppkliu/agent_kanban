import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TopBar from "./TopBar";
import { useStore } from "../store";

vi.mock("../api/client", () => ({
  api: {
    patchConfig: vi.fn().mockResolvedValue({ ok: true }),
    emergencyStop: vi.fn().mockResolvedValue({
      ok: true,
      aborted_count: 2,
      aborted_ids: ["id-MT-1", "id-MT-2"],
    }),
  },
  setApiKey: vi.fn(),
}));
vi.mock("../api/ws", () => ({
  dashboardSocket: {
    on: vi.fn(() => () => {}),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

import { api } from "../api/client";

beforeEach(() => {
  useStore.setState({
    status: "open",
    snapshot: {
      tick_at: "",
      config: {
        tracker_kind: "memory",
        tracker_repo: null,
        runner_kind: "echo",
        runner_model: "claude-opus-4-7",
        polling_interval_ms: 30000,
        max_concurrent_agents: 5,
      } as never,
      columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
      totals: { active_workers: 2, released_today: 7 },
    },
    workflowEditorOpen: false,
  });
  vi.clearAllMocks();
});
afterEach(() => {
  useStore.setState({ snapshot: null });
});

describe("TopBar", () => {
  it("shows tracker, runner, tick metadata", () => {
    render(<TopBar />);
    expect(screen.getByText(/tracker:/)).toBeInTheDocument();
    expect(screen.getByText("memory")).toBeInTheDocument();
    expect(screen.getByText(/echo · claude-opus-4-7/)).toBeInTheDocument();
    expect(screen.getByText(/30000ms/)).toBeInTheDocument();
  });

  it("displays active worker and released totals", () => {
    render(<TopBar />);
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("connection dot reflects status", () => {
    render(<TopBar />);
    const dot = document.querySelector(".rounded-full");
    expect(dot).toHaveClass("bg-emerald-400");
  });

  it("max_concurrent inline edit + Enter triggers patchConfig", async () => {
    const user = userEvent.setup();
    render(<TopBar />);
    await user.click(screen.getByRole("button", { name: "5" }));
    const input = screen.getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "9{Enter}");
    expect(api.patchConfig).toHaveBeenCalledWith({ max_concurrent_agents: 9 });
  });

  it("workflow toggle button toggles store flag", async () => {
    const user = userEvent.setup();
    render(<TopBar />);
    await user.click(screen.getByRole("button", { name: /Workflow/i }));
    expect(useStore.getState().workflowEditorOpen).toBe(true);
  });

  it("emergency stop calls api when confirmed", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<TopBar />);
    await user.click(
      screen.getByRole("button", { name: /Emergency stop all running agents/i }),
    );
    expect(confirmSpy).toHaveBeenCalled();
    expect(api.emergencyStop).toHaveBeenCalledWith("operator emergency stop");
    confirmSpy.mockRestore();
  });

  it("emergency stop is a no-op when user cancels confirm", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<TopBar />);
    await user.click(
      screen.getByRole("button", { name: /Emergency stop all running agents/i }),
    );
    expect(api.emergencyStop).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
