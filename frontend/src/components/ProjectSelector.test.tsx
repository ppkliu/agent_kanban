import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ProjectSelector from "./ProjectSelector";
import { useProjectStore } from "../projectStore";

vi.mock("../api/client", () => ({
  api: {
    listProjects: vi.fn(),
    createProject: vi.fn(),
    patchProject: vi.fn(),
  },
}));

function seedStore(partial: Partial<ReturnType<typeof useProjectStore.getState>>) {
  useProjectStore.setState({
    projects: [],
    selectedProjectId: null,
    loading: false,
    error: null,
    ...partial,
  } as never);
}

beforeEach(() => {
  seedStore({
    projects: [
      {
        id: "default",
        name: "Default project",
        created_at: "2026-05-16T00:00:00Z",
        archived_at: null,
      },
      {
        id: "proj_abc",
        name: "Alpha",
        created_at: "2026-05-16T00:01:00Z",
        archived_at: null,
      },
    ],
  });
});

afterEach(() => {
  seedStore({ projects: [], selectedProjectId: null });
});

describe("ProjectSelector", () => {
  it("shows 'All projects' label when nothing is selected", () => {
    render(<ProjectSelector />);
    expect(screen.getByText("All projects")).toBeInTheDocument();
  });

  it("clicking the trigger opens the dropdown listing every project", async () => {
    render(<ProjectSelector />);
    const trigger = screen.getByLabelText("Project selector");
    await userEvent.click(trigger);
    // Includes a virtual "All projects" entry AND every loaded project
    expect(screen.getAllByText("All projects")).toHaveLength(2); // label + menu entry
    expect(screen.getByText("Default project")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("+ New project…")).toBeInTheDocument();
  });

  it("selecting a project updates the store and closes the menu", async () => {
    render(<ProjectSelector />);
    await userEvent.click(screen.getByLabelText("Project selector"));
    await userEvent.click(screen.getByText("Alpha"));
    expect(useProjectStore.getState().selectedProjectId).toBe("proj_abc");
    // Menu closes — the "+ New project" entry should no longer be in the DOM
    expect(screen.queryByText("+ New project…")).not.toBeInTheDocument();
  });

  it("renders the project name (not id) in the trigger after selection", async () => {
    seedStore({
      projects: [
        {
          id: "proj_abc",
          name: "Alpha",
          created_at: "x",
          archived_at: null,
        },
      ],
      selectedProjectId: "proj_abc",
    });
    render(<ProjectSelector />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });
});
