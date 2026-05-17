import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TracePanel from "./TracePanel";
import { useStore } from "../store";
import { useProjectStore } from "../projectStore";

vi.mock("../api/ws", () => ({
  dashboardSocket: {
    on: vi.fn(() => () => {}),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

function seedSnapshot() {
  useStore.setState({
    snapshot: {
      tick_at: "",
      config: {} as never,
      columns: {
        pending: [
          {
            issue: {
              id: "id-A",
              identifier: "A",
              title: "alpha",
              labels: ["tool-api", "project:proj_a"],
            },
            attempt: null,
            queue_rank: null,
          },
          {
            issue: {
              id: "id-B",
              identifier: "B",
              title: "beta",
              labels: ["tool-api", "project:proj_b"],
            },
            attempt: null,
            queue_rank: null,
          },
          {
            issue: {
              id: "id-LEGACY",
              identifier: "L",
              title: "legacy",
              labels: ["tool-api"], // no project label
            },
            attempt: null,
            queue_rank: null,
          },
        ],
        claimed: [],
        running: [],
        retry_queued: [],
        released: [],
      },
      totals: { active_workers: 0, released_today: 0 } as never,
    } as never,
    activity: [
      {
        id: 1,
        issue_id: "id-A",
        kind: "message_delta",
        timestamp: "2026-05-17T01:23:45Z",
        summary: "→ hello from A",
      },
      {
        id: 2,
        issue_id: "id-B",
        kind: "message_delta",
        timestamp: "2026-05-17T01:23:46Z",
        summary: "→ hello from B",
      },
      {
        id: 3,
        issue_id: "id-LEGACY",
        kind: "message_delta",
        timestamp: "2026-05-17T01:23:47Z",
        summary: "→ hello from legacy",
      },
    ],
  } as never);
}

beforeEach(() => {
  seedSnapshot();
  useProjectStore.setState({
    projects: [],
    selectedProjectId: "proj_a",
    loading: false,
    error: null,
  });
});

afterEach(() => {
  useStore.setState({ snapshot: null, activity: [] } as never);
  useProjectStore.setState({ projects: [], selectedProjectId: null });
});

describe("TracePanel", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <TracePanel open={false} onClose={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows only events whose issue belongs to the current project", () => {
    render(<TracePanel open onClose={() => {}} />);
    expect(screen.getByText("→ hello from A")).toBeInTheDocument();
    expect(screen.queryByText("→ hello from B")).not.toBeInTheDocument();
    expect(screen.queryByText("→ hello from legacy")).not.toBeInTheDocument();
  });

  it("treats unlabeled legacy issues as belonging to 'default'", () => {
    useProjectStore.setState({
      projects: [],
      selectedProjectId: "default",
      loading: false,
      error: null,
    });
    render(<TracePanel open onClose={() => {}} />);
    expect(screen.getByText("→ hello from legacy")).toBeInTheDocument();
    // A and B both have explicit project labels — excluded
    expect(screen.queryByText("→ hello from A")).not.toBeInTheDocument();
    expect(screen.queryByText("→ hello from B")).not.toBeInTheDocument();
  });

  it("'All projects' checkbox bypasses the filter", async () => {
    render(<TracePanel open onClose={() => {}} />);
    // Pre-toggle: only A visible
    expect(screen.queryByText("→ hello from B")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("All projects"));
    expect(screen.getByText("→ hello from A")).toBeInTheDocument();
    expect(screen.getByText("→ hello from B")).toBeInTheDocument();
    expect(screen.getByText("→ hello from legacy")).toBeInTheDocument();
  });
});
