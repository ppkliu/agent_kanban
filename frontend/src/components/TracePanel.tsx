import { useMemo, useState } from "react";
import { useStore } from "../store";
import { useProjectStore, effectiveSubmitProjectId } from "../projectStore";

interface Props {
  open: boolean;
  onClose: () => void;
}

/** TracePanel — live trace of agent events filtered by current project.
 *
 * Reads the store's existing `activity` ring (already populated by the
 * dashboard's single WebSocket connection) and derives the issue → project
 * mapping from the most recent state_snapshot. Cheap to render; updates
 * automatically as new agent_event messages flow in via WS.
 *
 * Headless callers wanting a server-side filter can use the WS
 * `?filter=project:<id>` query (Phase E4 backend extension). The
 * dashboard's main connection stays unfiltered so the kanban + chat
 * surfaces all share one WebSocket.
 */
export default function TracePanel({ open, onClose }: Props) {
  const activity = useStore((s) => s.activity);
  const snapshot = useStore((s) => s.snapshot);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);
  const [showAll, setShowAll] = useState(false);

  const projectKey = effectiveSubmitProjectId(selectedProjectId);

  // Build issue_id → project lookup from the current snapshot. Legacy
  // issues without a `project:` label fall back to `default`, matching
  // the backend kanban filter + WS project filter semantics.
  const issueProject = useMemo(() => {
    const out = new Map<string, string>();
    if (!snapshot) return out;
    for (const col of Object.values(snapshot.columns)) {
      for (const entry of col) {
        const labels = entry.issue.labels ?? [];
        let proj: string | null = null;
        for (const l of labels) {
          if (l.startsWith("project:")) {
            proj = l.slice("project:".length);
            break;
          }
        }
        out.set(entry.issue.id, proj ?? "default");
      }
    }
    return out;
  }, [snapshot]);

  const filtered = useMemo(() => {
    if (showAll) return activity;
    return activity.filter((item) => {
      const proj = issueProject.get(item.issue_id) ?? "default";
      return proj === projectKey;
    });
  }, [activity, issueProject, projectKey, showAll]);

  if (!open) return null;

  return (
    <aside
      role="dialog"
      aria-label="Per-project trace"
      className="fixed bottom-4 left-4 z-40 w-[440px] max-w-[95vw] h-[640px] max-h-[80vh] bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-2 border-b border-zinc-800">
        <h2 className="text-sm font-semibold">
          🔍 Trace · <span className="text-zinc-400">{projectKey}</span>
        </h2>
        <div className="flex items-center gap-2">
          <label className="text-[10px] text-zinc-400 inline-flex items-center gap-1">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(e) => setShowAll(e.target.checked)}
            />
            All projects
          </label>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 text-lg leading-none px-1"
            aria-label="Close trace panel"
          >
            ×
          </button>
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-auto px-3 py-2 text-xs font-mono">
        {filtered.length === 0 ? (
          <p className="text-zinc-500 font-sans p-3">
            No recent events for this project yet. As agents run on tasks
            scoped to <span className="text-zinc-200">{projectKey}</span>,
            their events stream here in real time.
          </p>
        ) : (
          <ul className="space-y-1">
            {filtered.map((item) => (
              <li
                key={item.id}
                className="border-l-2 border-zinc-800 pl-2 py-0.5"
              >
                <div className="flex items-baseline gap-2 text-zinc-500">
                  <span className="text-[10px]">
                    {item.timestamp.slice(11, 19)}
                  </span>
                  <span className="text-zinc-400 text-[10px] truncate">
                    {item.issue_id}
                  </span>
                </div>
                <div className="text-zinc-200 break-words">
                  {item.summary}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <footer className="border-t border-zinc-800 px-3 py-2 text-[10px] text-zinc-500">
        {filtered.length} of {activity.length} events shown ·{" "}
        {showAll
          ? "across all projects"
          : `filtered to project ${projectKey}`}
      </footer>
    </aside>
  );
}
