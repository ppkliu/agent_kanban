import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FilterBar from "./FilterBar";
import { useStore } from "../store";

function snapshotWithLabels(labelSets: string[][]) {
  return {
    tick_at: "",
    config: {} as never,
    columns: {
      pending: labelSets.map((labels, i) => ({
        issue: { id: `id-${i}`, identifier: `MT-${i}`, labels },
        attempt: null,
        queue_rank: null,
      })),
      claimed: [],
      running: [],
      retry_queued: [],
      released: [],
    },
    totals: { active_workers: 0, released_today: 0 },
  };
}

beforeEach(() => {
  useStore.setState({
    filters: { text: "", agent: null, priority: null, label: null },
    snapshot: snapshotWithLabels([
      ["a11y", "frontend"],
      ["backend"],
    ]),
  });
});
afterEach(() => {
  // restore between tests
  useStore.setState({
    filters: { text: "", agent: null, priority: null, label: null },
    snapshot: null,
  });
});

describe("FilterBar", () => {
  it("typing in the search box updates filters.text", async () => {
    const user = userEvent.setup();
    render(<FilterBar />);
    const input = screen.getByPlaceholderText(/search title/i);
    await user.type(input, "MT-1");
    expect(useStore.getState().filters.text).toBe("MT-1");
  });

  it("computes the label dropdown options as union from snapshot", () => {
    render(<FilterBar />);
    expect(screen.getByRole("option", { name: "a11y" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "frontend" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "backend" })).toBeInTheDocument();
  });

  it("changing label dropdown updates filters.label", () => {
    render(<FilterBar />);
    const labelSelect = screen.getAllByRole("combobox")[1];
    fireEvent.change(labelSelect, { target: { value: "a11y" } });
    expect(useStore.getState().filters.label).toBe("a11y");
  });

  it("clear filters button hidden when nothing is set", () => {
    render(<FilterBar />);
    expect(screen.queryByText(/clear filters/i)).not.toBeInTheDocument();
  });

  it("clear filters button shown when text is set", () => {
    useStore.setState({
      filters: { text: "X", agent: null, priority: null, label: null },
    });
    render(<FilterBar />);
    expect(screen.getByText(/clear filters/i)).toBeInTheDocument();
  });

  it("clicking 'clear filters' resets all filter fields", async () => {
    useStore.setState({
      filters: { text: "MT", agent: null, priority: 2, label: "a11y" },
    });
    const user = userEvent.setup();
    render(<FilterBar />);
    await user.click(screen.getByText(/clear filters/i));
    const f = useStore.getState().filters;
    expect(f.text).toBe("");
    expect(f.priority).toBeNull();
    expect(f.label).toBeNull();
  });
});
