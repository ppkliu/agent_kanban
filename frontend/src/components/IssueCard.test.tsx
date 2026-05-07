import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import IssueCard from "./IssueCard";
import { useStore } from "../store";
import type { AttemptDTO, KanbanEntry, RunStateStr } from "../api/types";

vi.mock("../api/client", () => ({
  api: {
    pause: vi.fn().mockResolvedValue({ ok: true }),
    resume: vi.fn().mockResolvedValue({ ok: true }),
    abort: vi.fn().mockResolvedValue({ ok: true }),
    retry: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

vi.mock("../api/ws", () => ({
  dashboardSocket: {
    on: vi.fn(() => () => {}),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

function makeEntry(overrides: {
  id?: string;
  identifier?: string;
  title?: string;
  priority?: number;
  labels?: string[];
  attempt?: Partial<AttemptDTO> | null;
  queue_rank?: number | null;
}): KanbanEntry {
  return {
    issue: {
      id: overrides.id ?? "id-MT-1",
      identifier: overrides.identifier ?? "MT-1",
      title: overrides.title ?? "Refactor login form",
      priority: overrides.priority ?? 1,
      labels: overrides.labels ?? [],
    },
    attempt:
      overrides.attempt === null
        ? null
        : ({
            issue_id: "id-MT-1",
            attempt_number: 1,
            state: "running" as RunStateStr,
            started_at: new Date(Date.now() - 30_000).toISOString(),
            ended_at: null,
            terminal_reason: null,
            last_event_at: null,
            session_id: "s1",
            turns_consumed: 4,
            cost_usd: 0.0123,
            error_message: null,
            retry_after: null,
            paused_until: null,
            ...(overrides.attempt ?? {}),
          } as AttemptDTO),
    queue_rank: overrides.queue_rank ?? null,
  };
}

beforeEach(() => {
  useStore.setState({
    snapshot: {
      tick_at: "",
      config: { max_turns: 20 } as never,
      columns: { pending: [], claimed: [], running: [], retry_queued: [], released: [] },
      totals: { active_workers: 0, released_today: 0 },
    },
    selectedIssueId: null,
    notice: null,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("IssueCard", () => {
  it("renders identifier, title, priority and labels", () => {
    render(
      <IssueCard
        entry={makeEntry({ labels: ["a11y", "frontend"] })}
      />,
    );
    expect(screen.getByText("MT-1")).toBeInTheDocument();
    expect(screen.getByText("Refactor login form")).toBeInTheDocument();
    expect(screen.getByText("p:1")).toBeInTheDocument();
    expect(screen.getByText("a11y")).toBeInTheDocument();
    expect(screen.getByText("frontend")).toBeInTheDocument();
  });

  it("shows turn progress and cost when attempt exists", () => {
    render(
      <IssueCard
        entry={makeEntry({ attempt: { turns_consumed: 6, cost_usd: 0.025 } })}
      />,
    );
    expect(screen.getByText(/6\/20 turns/)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.0250/)).toBeInTheDocument();
  });

  it("shows queue rank instead of progress when attempt is null", () => {
    render(
      <IssueCard entry={makeEntry({ attempt: null, queue_rank: 3 })} />,
    );
    expect(screen.getByText(/queue rank: 3/i)).toBeInTheDocument();
    expect(screen.queryByText(/turns/)).not.toBeInTheDocument();
  });

  it("clicking the card calls selectIssue with the issue id", async () => {
    const user = userEvent.setup();
    render(<IssueCard entry={makeEntry({})} />);
    await user.click(screen.getByText("Refactor login form"));
    expect(useStore.getState().selectedIssueId).toBe("id-MT-1");
  });

  it("right-click opens context menu and Pause is enabled for running attempt", async () => {
    const user = userEvent.setup();
    render(<IssueCard entry={makeEntry({})} />);
    await user.pointer({
      keys: "[MouseRight]",
      target: screen.getByText("Refactor login form"),
    });
    expect(screen.getByText("⏸ Pause")).toBeInTheDocument();
    expect(screen.getByText("⏸ Pause")).not.toBeDisabled();
  });

  it("Retry button is enabled only for released attempt", async () => {
    const user = userEvent.setup();
    render(
      <IssueCard
        entry={makeEntry({ attempt: { state: "released" as RunStateStr } })}
      />,
    );
    await user.pointer({
      keys: "[MouseRight]",
      target: screen.getByText("Refactor login form"),
    });
    expect(screen.getByText("↻ Retry")).not.toBeDisabled();
    expect(screen.getByText("⏸ Pause")).toBeDisabled();
  });
});
