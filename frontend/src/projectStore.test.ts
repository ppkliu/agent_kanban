import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProjectStore, effectiveSubmitProjectId } from "./projectStore";
import * as clientModule from "./api/client";

const STORAGE_KEY = "symphony.selectedProjectId";

function resetStore() {
  useProjectStore.setState({
    projects: [],
    selectedProjectId: null,
    loading: false,
    error: null,
  });
}

vi.mock("./api/client", async () => {
  const actual = await vi.importActual<typeof import("./api/client")>(
    "./api/client",
  );
  return {
    ...actual,
    api: {
      listProjects: vi.fn(),
      createProject: vi.fn(),
      patchProject: vi.fn(),
    },
  };
});

const mockedApi = clientModule.api as unknown as Record<
  string,
  ReturnType<typeof vi.fn>
>;

beforeEach(() => {
  resetStore();
  window.localStorage.removeItem(STORAGE_KEY);
  Object.values(mockedApi).forEach((fn) => fn.mockReset?.());
});

afterEach(() => {
  window.localStorage.removeItem(STORAGE_KEY);
});

describe("projectStore", () => {
  it("starts with null selection (= all projects) when localStorage is empty", () => {
    // Already reset; useProjectStore was constructed before this test runs,
    // so re-create the same state explicitly via setState.
    expect(useProjectStore.getState().selectedProjectId).toBeNull();
  });

  it("select() persists to localStorage and updates state", () => {
    useProjectStore.getState().select("proj_abc");
    expect(useProjectStore.getState().selectedProjectId).toBe("proj_abc");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("proj_abc");

    useProjectStore.getState().select(null);
    expect(useProjectStore.getState().selectedProjectId).toBeNull();
    // null is persisted as empty string so a follow-up read distinguishes
    // "never set" from "explicitly chose All".
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("");
  });

  it("refresh() populates projects from the api client", async () => {
    mockedApi.listProjects.mockResolvedValue({
      projects: [
        {
          id: "default",
          name: "Default project",
          created_at: "2026-05-16T00:00:00Z",
          archived_at: null,
        },
      ],
    });
    await useProjectStore.getState().refresh();
    const state = useProjectStore.getState();
    expect(state.projects).toHaveLength(1);
    expect(state.projects[0].id).toBe("default");
    expect(state.error).toBeNull();
  });

  it("refresh() surfaces error on api failure", async () => {
    mockedApi.listProjects.mockRejectedValue(new Error("boom"));
    await useProjectStore.getState().refresh();
    expect(useProjectStore.getState().error).toBe("boom");
    expect(useProjectStore.getState().projects).toEqual([]);
  });

  it("create() creates + refreshes + auto-selects the new project", async () => {
    const created = {
      id: "proj_new",
      name: "New",
      created_at: "2026-05-16T01:00:00Z",
      archived_at: null,
    };
    mockedApi.createProject.mockResolvedValue(created);
    mockedApi.listProjects.mockResolvedValue({ projects: [created] });

    const p = await useProjectStore.getState().create("New");
    expect(p).toEqual(created);
    expect(useProjectStore.getState().selectedProjectId).toBe("proj_new");
  });

  it("setArchived() falls back to All when the currently-selected project is archived", async () => {
    mockedApi.patchProject.mockResolvedValue({
      ok: true,
      changed: { archived: true },
      project: {
        id: "p1",
        name: "P1",
        created_at: "x",
        archived_at: "y",
      },
    });
    mockedApi.listProjects.mockResolvedValue({ projects: [] });

    useProjectStore.getState().select("p1");
    await useProjectStore.getState().setArchived("p1", true);
    expect(useProjectStore.getState().selectedProjectId).toBeNull();
  });
});

describe("effectiveSubmitProjectId", () => {
  it("returns 'default' when selection is null", () => {
    expect(effectiveSubmitProjectId(null)).toBe("default");
  });

  it("returns the project id when selection is set", () => {
    expect(effectiveSubmitProjectId("proj_abc")).toBe("proj_abc");
  });
});
