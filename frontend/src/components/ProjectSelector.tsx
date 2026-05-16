import { useEffect, useRef, useState } from "react";
import { useProjectStore } from "../projectStore";

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

  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

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
          className="absolute top-full left-0 mt-1 z-30 w-64 max-h-80 overflow-auto rounded border border-zinc-700 bg-zinc-900 shadow-2xl text-xs"
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
            </button>
          </li>
          <li className="border-t border-zinc-800" />
          {projects.length === 0 ? (
            <li className="px-3 py-2 text-zinc-500">
              No projects yet — create one below
            </li>
          ) : (
            projects.map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => {
                    select(p.id);
                    setOpen(false);
                  }}
                  className={`w-full text-left px-3 py-2 hover:bg-zinc-800 ${
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
              </li>
            ))
          )}
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
