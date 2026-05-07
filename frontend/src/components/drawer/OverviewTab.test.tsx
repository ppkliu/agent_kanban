import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OverviewTab from "./OverviewTab";
import type { IssueDetail, RunStateStr } from "../../api/types";

vi.mock("../../api/client", () => ({
  api: {
    pause: vi.fn().mockResolvedValue({ ok: true }),
    resume: vi.fn().mockResolvedValue({ ok: true }),
    abort: vi.fn().mockResolvedValue({ ok: true }),
    retry: vi.fn().mockResolvedValue({ ok: true }),
  },
}));
vi.mock("../../api/ws", () => ({
  dashboardSocket: {
    on: vi.fn(() => () => {}),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

import { api } from "../../api/client";

function makeDetail(opts: {
  state?: RunStateStr;
  paused_until?: string | null;
  history?: Array<{
    attempt_number: number;
    state: string;
    terminal_reason: string | null;
    turns_consumed: number;
    cost_usd: number;
    started_at: string | null;
    ended_at: string | null;
  }>;
} = {}): IssueDetail {
  return {
    issue: {
      id: "id-MT-1",
      identifier: "MT-1",
      title: "Refactor",
      description: "Body",
      labels: ["a11y"],
    },
    current_attempt: opts.state
      ? {
          issue_id: "id-MT-1",
          attempt_number: 2,
          state: opts.state,
          started_at: null,
          ended_at: null,
          terminal_reason: null,
          last_event_at: null,
          session_id: null,
          turns_consumed: 0,
          cost_usd: 0,
          error_message: null,
          retry_after: null,
          paused_until: opts.paused_until ?? null,
        }
      : null,
    all_attempts: (opts.history ?? []).map((h) => ({
      issue_id: "id-MT-1",
      session_id: null,
      last_event_at: null,
      error_message: null,
      ...h,
    })),
    hints: [],
    rendered_prompt_preview: null,
    workspace_path: null,
    tracker_url: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => {});

describe("OverviewTab", () => {
  it("renders description and labels", () => {
    render(<OverviewTab detail={makeDetail({ state: "running" })} onMutated={() => {}} />);
    expect(screen.getByText("Body")).toBeInTheDocument();
    expect(screen.getByText("a11y")).toBeInTheDocument();
  });

  it("Pause button is enabled for running attempt", () => {
    render(<OverviewTab detail={makeDetail({ state: "running" })} onMutated={() => {}} />);
    expect(screen.getByRole("button", { name: /Pause/ })).not.toBeDisabled();
  });

  it("Pause button disabled when there is no current attempt", () => {
    render(<OverviewTab detail={makeDetail({})} onMutated={() => {}} />);
    expect(screen.getByRole("button", { name: /Pause/ })).toBeDisabled();
  });

  it("Resume button enabled only when paused_until is set", () => {
    render(
      <OverviewTab
        detail={makeDetail({ state: "retry_queued", paused_until: "2999-12-31T00:00:00Z" })}
        onMutated={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /Resume/ })).not.toBeDisabled();
  });

  it("Force retry button enabled only for released attempt", () => {
    render(
      <OverviewTab
        detail={makeDetail({ state: "released" })}
        onMutated={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /Force retry/ })).not.toBeDisabled();
  });

  it("clicking Pause calls api.pause and onMutated", async () => {
    const onMutated = vi.fn();
    const user = userEvent.setup();
    render(<OverviewTab detail={makeDetail({ state: "running" })} onMutated={onMutated} />);
    await user.click(screen.getByRole("button", { name: /Pause/ }));
    expect(api.pause).toHaveBeenCalledWith("id-MT-1");
    expect(onMutated).toHaveBeenCalled();
  });

  it("renders historical attempts with terminal_reason and cost columns", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          history: [
            {
              attempt_number: 1,
              state: "released",
              terminal_reason: "agent_finished",
              turns_consumed: 4,
              cost_usd: 0.0123,
              started_at: "2026-05-06T03:14:00Z",
              ended_at: "2026-05-06T03:15:00Z",
            },
          ],
        })}
        onMutated={() => {}}
      />,
    );
    expect(screen.getByText(/agent_finished/)).toBeInTheDocument();
    expect(screen.getByText("$0.0123")).toBeInTheDocument();
  });
});
