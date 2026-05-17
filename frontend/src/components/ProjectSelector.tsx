import { useEffect, useMemo, useRef, useState } from "react";
import { useProjectStore } from "../projectStore";
import type { ProjectDTO } from "../api/types";

/** Project selector — sits leftmost in the TopBar.
 *
 * Three states:
 *   - "All projects" (selectedProjectId === null) — kanban shows everything
 *   - a specific active project — kanban filters to it
 *   - "+ New project…" prompts for a name, POSTs, switches to the new one
 *
 * Refreshes the project list on mount via the WebSocket-driven store init
 * (App.tsx triggers refresh once after WS connect). After any local create /
 * rename / archive the store re-fetches.
 */
export default function ProjectSelector() {
  const projects = useProjectStore((s) => s.projects);
  const selected = useProjectStore((s) => s.selectedProjectId);
  const select = useProjectStore((s) => s.select);
  const createProject = useProjectStore((s) => s.create);
  const setArchived = useProjectStore((s) => s.setArchived);

  const [open, setOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const { active, archived } = useMemo(() => {
    const a: ProjectDTO[] = [];
    const z: ProjectDTO[] = [];
    for (const p of projects) {
      if (p.archived_at) z.push(p);
      else a.push(p);
    }
    return { active: a, archived: z };
  }, [projects]);

  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const handleCreate = async () => {
    const name = window.prompt("New project name:");
    if (!name || name.trim().length === 0) return;
    setOpen(false);
    await createProject(name.trim());
  };

  const handleArchive = async (e: React.MouseEvent, p: ProjectDTO) => {
    e.stopPropagation();
    if (p.id === "default") {
      // The default project is the auto-attached fallback — archiving it
      // would orphan untagged legacy issues. Disallow at UI level (backend
      // would accept it but the UX confusion isn't worth it).
      window.alert("The default project cannot be archived.");
      return;
    }
    const verb = p.archived_at ? "unarchive" : "archive";
    if (!window.confirm(`${verb} project "${p.name}"?`)) return;
    await setArchived(p.id, !p.archived_at);
  };

  const currentLabel =
    selected === null
      ? "All projects"
      : (projects.find((p) => p.id === selected)?.name ?? selected);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="px-3 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 inline-flex items-center gap-2"
        title="Switch project (kanban + chat scope)"
        aria-label="Project selector"
        aria-expanded={open}
      >
        <span className="text-zinc-400">project:</span>
        <span className="text-zinc-100 max-w-[180px] truncate">
          {currentLabel}
        </span>
        <span className="text-zinc-500">▾</span>
      </button>
      {open ? (
        <ul
          role="menu"
          aria-label="Available projects"
          className="absolute top-full left-0 mt-1 z-30 w-72 max-h-96 overflow-auto rounded border border-zinc-700 bg-zinc-900 shadow-2xl text-xs"
        >
          <li>
            <button
              onClick={() => {
                select(null);
                setOpen(false);
              }}
              className={`w-full text-left px-3 py-2 hover:bg-zinc-800 ${
                selected === null ? "bg-zinc-800 text-emerald-300" : "text-zinc-200"
              }`}
              role="menuitem"
            >
              All projects
              <span className="ml-2 text-zinc-500 text-[10px]">
                (cross-project view)
              </span>
            </button>
          </li>
          <li className="border-t border-zinc-800" />
          {active.length === 0 ? (
            <li className="px-3 py-2 text-zinc-500">
              No active projects yet — create one below
            </li>
          ) : (
            active.map((p) => (
              <li key={p.id} className="flex items-stretch">
                <button
                  onClick={() => {
                    select(p.id);
                    setOpen(false);
                  }}
                  className={`flex-1 min-w-0 text-left px-3 py-2 hover:bg-zinc-800 ${
                    selected === p.id
                      ? "bg-zinc-800 text-emerald-300"
                      : "text-zinc-200"
                  }`}
                  role="menuitem"
                  title={`id: ${p.id}`}
                >
                  <div className="truncate">{p.name}</div>
                  <div className="text-zinc-500 text-[10px] font-mono truncate">
                    {p.id}
                  </div>
                </button>
                {p.id !== "default" ? (
                  <button
                    onClick={(e) => handleArchive(e, p)}
                    className="px-2 text-zinc-500 hover:text-amber-300 hover:bg-zinc-800"
                    title={`Archive ${p.name}`}
                    aria-label={`Archive project ${p.name}`}
                  >
                    📦
                  </button>
                ) : null}
              </li>
            ))
          )}
          {archived.length > 0 ? (
            <>
              <li className="border-t border-zinc-800" />
              <li>
                <button
                  onClick={() => setShowArchived((v) => !v)}
                  className="w-full text-left px-3 py-2 text-zinc-400 hover:bg-zinc-800 flex items-center justify-between"
                >
                  <span>Archived ({archived.length})</span>
                  <span className="text-zinc-500">{showArchived ? "▾" : "▸"}</span>
                </button>
              </li>
              {showArchived
                ? archived.map((p) => (
                    <li key={p.id} className="flex items-stretch opacity-60">
                      <button
                        onClick={() => {
                          select(p.id);
                          setOpen(false);
                        }}
                        className={`flex-1 min-w-0 text-left px-3 py-2 hover:bg-zinc-800 ${
                          selected === p.id
                            ? "bg-zinc-800 text-emerald-300"
                            : "text-zinc-300"
                        }`}
                        role="menuitem"
                        title={`id: ${p.id} — archived`}
                      >
                        <div className="truncate line-through decoration-zinc-600">
                          {p.name}
                        </div>
                        <div className="text-zinc-500 text-[10px] font-mono truncate">
                          {p.id}
                        </div>
                      </button>
                      <button
                        onClick={(e) => handleArchive(e, p)}
                        className="px-2 text-zinc-500 hover:text-emerald-300 hover:bg-zinc-800"
                        title={`Unarchive ${p.name}`}
                        aria-label={`Unarchive project ${p.name}`}
                      >
                        ↩
                      </button>
                    </li>
                  ))
                : null}
            </>
          ) : null}
          <li className="border-t border-zinc-800" />
          <li>
            <button
              onClick={handleCreate}
              className="w-full text-left px-3 py-2 hover:bg-zinc-800 text-emerald-300"
              role="menuitem"
            >
              + New project…
            </button>
          </li>
        </ul>
      ) : null}
    </div>
  );
}
