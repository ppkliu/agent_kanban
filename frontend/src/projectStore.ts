/** Project state slice (Phase E2) — current project_id + project list.
 *
 * Kept as its own Zustand store so the main `useStore` snapshot stays
 * focused on orchestrator state pushed via WebSocket; this slice's
 * lifecycle is driven by REST (listProjects / createProject /
 * patchProject) and localStorage persistence.
 *
 * Selection model: "default" is the sentinel for the auto-default project
 * the backend always materialises. The selector also exposes a virtual
 * "all" option (selectedProjectId === null) that disables the kanban
 * filter and shows every project's tasks. The Chat panel and Tool API
 * submissions use the *resolved* `selectedProjectId ?? "default"`.
 */
import { create } from "zustand";
import { api } from "./api/client";
import type { ProjectDTO } from "./api/types";

const SELECTION_KEY = "symphony.selectedProjectId";

export type ProjectSelection = string | null; // null => "all projects"

function readStoredSelection(): ProjectSelection {
  if (typeof window === "undefined") return null;
  const v = window.localStorage.getItem(SELECTION_KEY);
  if (v === null) return null;
  return v === "" ? null : v;
}

function writeStoredSelection(v: ProjectSelection): void {
  if (typeof window === "undefined") return;
  if (v === null) window.localStorage.setItem(SELECTION_KEY, "");
  else window.localStorage.setItem(SELECTION_KEY, v);
}

interface ProjectStore {
  projects: ProjectDTO[];
  /** null = "All projects" (no filter applied). Otherwise the project id. */
  selectedProjectId: ProjectSelection;
  loading: boolean;
  error: string | null;

  refresh: () => Promise<void>;
  select: (id: ProjectSelection) => void;
  create: (name: string) => Promise<ProjectDTO | null>;
  rename: (id: string, name: string) => Promise<void>;
  setArchived: (id: string, archived: boolean) => Promise<void>;
}

export const useProjectStore = create<ProjectStore>((set, get) => ({
  projects: [],
  selectedProjectId: readStoredSelection(),
  loading: false,
  error: null,

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const r = await api.listProjects();
      set({ projects: r.projects, loading: false });
    } catch (e) {
      set({ loading: false, error: (e as Error).message });
    }
  },

  select: (id) => {
    writeStoredSelection(id);
    set({ selectedProjectId: id });
  },

  create: async (name) => {
    try {
      const p = await api.createProject({ name });
      // Refresh list, then select the new one.
      await get().refresh();
      get().select(p.id);
      return p;
    } catch (e) {
      set({ error: (e as Error).message });
      return null;
    }
  },

  rename: async (id, name) => {
    try {
      await api.patchProject(id, { name });
      await get().refresh();
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  setArchived: async (id, archived) => {
    try {
      await api.patchProject(id, { archived });
      await get().refresh();
      // If the currently selected project just got archived, fall back to "All".
      if (archived && get().selectedProjectId === id) get().select(null);
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },
}));

/** Resolve the effective project_id used when submitting a new task —
 * either the user-selected one, or the backend's auto-default. Components
 * that POST to submit_coding_task should call this to avoid sending an
 * empty selection. */
export function effectiveSubmitProjectId(
  selected: ProjectSelection,
): string {
  return selected ?? "default";
}
