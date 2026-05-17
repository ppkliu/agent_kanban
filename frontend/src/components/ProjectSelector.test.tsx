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

  // ----- Phase E5: archive / unarchive controls -----

  it("hides archived projects under a collapsible 'Archived' section", async () => {
    seedStore({
      projects: [
        {
          id: "default",
          name: "Default project",
          created_at: "x",
          archived_at: null,
        },
        {
          id: "proj_active",
          name: "Active alpha",
          created_at: "y",
          archived_at: null,
        },
        {
          id: "proj_old",
          name: "Old beta",
          created_at: "z",
          archived_at: "2026-05-17T00:00:00Z",
        },
      ],
    });
    render(<ProjectSelector />);
    await userEvent.click(screen.getByLabelText("Project selector"));

    // Active sees both default + Active alpha
    expect(screen.getByText("Active alpha")).toBeInTheDocument();
    // Archived section header advertises the count
    expect(screen.getByText("Archived (1)")).toBeInTheDocument();
    // The archived project itself is hidden until expanded
    expect(screen.queryByText("Old beta")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("Archived (1)"));
    expect(screen.getByText("Old beta")).toBeInTheDocument();
  });

  it("clicking the archive icon on a project sets it archived via the store", async () => {
    const setArchivedSpy = vi.fn().mockResolvedValue(undefined);
    seedStore({
      projects: [
        {
          id: "proj_abc",
          name: "Alpha",
          created_at: "x",
          archived_at: null,
        },
      ],
    });
    // Inject our spy without re-creating the store (would lose subscribers
    // attached by other tests). Just override the `setArchived` action.
    useProjectStore.setState({ setArchived: setArchivedSpy } as never);

    // Mock window.confirm to auto-approve
    const origConfirm = window.confirm;
    window.confirm = vi.fn(() => true);
    try {
      render(<ProjectSelector />);
      await userEvent.click(screen.getByLabelText("Project selector"));
      await userEvent.click(screen.getByLabelText("Archive project Alpha"));
      expect(setArchivedSpy).toHaveBeenCalledWith("proj_abc", true);
    } finally {
      window.confirm = origConfirm;
    }
  });

  it("default project has no archive icon (can't be archived from UI)", async () => {
    seedStore({
      projects: [
        {
          id: "default",
          name: "Default project",
          created_at: "x",
          archived_at: null,
        },
      ],
    });
    render(<ProjectSelector />);
    await userEvent.click(screen.getByLabelText("Project selector"));
    expect(
      screen.queryByLabelText("Archive project Default project"),
    ).not.toBeInTheDocument();
  });
});
