import { memo, useState } from "react";
import { useStore } from "../store";
import { useProjectStore } from "../projectStore";
import { api } from "../api/client";
import type { KanbanEntry } from "../api/types";

interface Props {
  entry: KanbanEntry;
  draggable?: boolean;
}

function formatRuntime(entry: KanbanEntry): string {
  const att = entry.attempt;
  if (!att?.started_at) return "—";
  const started = new Date(att.started_at).getTime();
  const ended = att.ended_at ? new Date(att.ended_at).getTime() : Date.now();
  const ms = Math.max(0, ended - started);
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function ContextMenu({
  entry,
  onClose,
}: {
  entry: KanbanEntry;
  onClose: () => void;
}) {
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);
  const id = entry.issue.id;
  const att = entry.attempt;
  const can = {
    pause: att?.state === "claimed" || att?.state === "running",
    resume:
      att?.state === "retry_queued" && att?.paused_until !== null,
    abort: att?.state === "claimed" || att?.state === "running",
    retry: att?.state === "released",
  };

  async function run(label: string, fn: () => Promise<unknown>) {
    try {
      await fn();
      setNotice({ kind: "info", text: `${label} ok (${entry.issue.identifier ?? id})` });
      void refresh();
    } catch (e) {
      setNotice({ kind: "error", text: `${label} failed: ${(e as Error).message}` });
    } finally {
      onClose();
    }
  }

  return (
    <div className="absolute z-30 right-2 top-2 bg-zinc-800 border border-zinc-700 rounded shadow-lg text-xs overflow-hidden min-w-32">
      <button
        disabled={!can.pause}
        onClick={() => run("pause", () => api.pause(id))}
        className="w-full text-left px-3 py-1.5 hover:bg-zinc-700 disabled:text-zinc-600 disabled:hover:bg-zinc-800"
      >
        ⏸ Pause
      </button>
      <button
        disabled={!can.resume}
        onClick={() => run("resume", () => api.resume(id))}
        className="w-full text-left px-3 py-1.5 hover:bg-zinc-700 disabled:text-zinc-600 disabled:hover:bg-zinc-800"
      >
        ▶ Resume
      </button>
      <button
        disabled={!can.abort}
        onClick={() => {
          if (!confirm(`Abort ${entry.issue.identifier}?`)) {
            onClose();
            return;
          }
          void run("abort", () => api.abort(id, "operator aborted"));
        }}
        className="w-full text-left px-3 py-1.5 hover:bg-zinc-700 text-rose-300 disabled:text-zinc-600 disabled:hover:bg-zinc-800"
      >
        ✕ Abort
      </button>
      <button
        disabled={!can.retry}
        onClick={() => run("retry", () => api.retry(id))}
        className="w-full text-left px-3 py-1.5 hover:bg-zinc-700 disabled:text-zinc-600 disabled:hover:bg-zinc-800"
      >
        ↻ Retry
      </button>
    </div>
  );
}

function IssueCardInner({ entry }: Props) {
  const select = useStore((s) => s.selectIssue);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);
  const projects = useProjectStore((s) => s.projects);
  const recentEvents = useStore((s) => s.recentEvents[entry.issue.id]);
  const [menuOpen, setMenuOpen] = useState(false);
  const att = entry.attempt;
  const issue = entry.issue;
  const cost = att?.cost_usd ?? 0;

  const latestCheckpoint = (() => {
    if (!recentEvents) return null;
    for (let i = recentEvents.length - 1; i >= 0; i--) {
      if (recentEvents[i].kind === "checkpoint") return recentEvents[i];
    }
    return null;
  })();

  const turnsTotal = useStore((s) => s.snapshot?.config.max_turns ?? 20);
  const turns = att?.turns_consumed ?? 0;
  const progress = Math.min(1, turns / turnsTotal);

  // Cross-project audit chip — only shown when the user is viewing "All
  // projects" (no filter active). Helps distinguish whose card is whose
  // when the kanban shows tasks from multiple projects at once.
  const projectLabel = issue.labels?.find((l) => l.startsWith("project:"));
  const projectId = projectLabel
    ? projectLabel.slice("project:".length)
    : "default";
  const showProjectChip = selectedProjectId === null;
  const projectName =
    projects.find((p) => p.id === projectId)?.name ?? projectId;

  const stateColor =
    att?.state === "running"
      ? "border-emerald-700/60"
      : att?.state === "claimed"
        ? "border-amber-700/60"
        : att?.state === "retry_queued"
          ? "border-orange-700/60"
          : att?.state === "released"
            ? "border-zinc-700"
            : "border-zinc-700/60";

  return (
    <div
      className={`relative bg-zinc-900 hover:bg-zinc-800/90 border ${stateColor} rounded p-2.5 cursor-pointer transition group`}
      onClick={() => select(issue.id)}
      onContextMenu={(e) => {
        e.preventDefault();
        setMenuOpen((v) => !v);
      }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-mono text-zinc-400">
          {issue.identifier}
        </span>
        <div className="flex items-center gap-1.5">
          {showProjectChip ? (
            <span
              className="text-[10px] bg-zinc-800 border border-zinc-700 rounded px-1.5 py-0.5 text-zinc-300 max-w-[120px] truncate"
              title={`project: ${projectId}`}
            >
              📁 {projectName}
            </span>
          ) : null}
          <span className="text-[10px] text-zinc-500">
            p:{issue.priority ?? "—"}
          </span>
        </div>
      </div>
      <div className="text-sm leading-tight mt-1 line-clamp-2">
        {issue.title || "(no title)"}
      </div>
      {issue.labels && issue.labels.length > 0 ? (
        <div className="flex flex-wrap gap-1 mt-2">
          {issue.labels.slice(0, 3).map((l) => (
            <span
              key={l}
              className="text-[10px] bg-zinc-800 border border-zinc-700 rounded px-1.5 py-0.5 text-zinc-300"
            >
              {l}
            </span>
          ))}
          {issue.labels.length > 3 && (
            <span className="text-[10px] text-zinc-500">
              +{issue.labels.length - 3}
            </span>
          )}
        </div>
      ) : null}
      {latestCheckpoint ? (() => {
        const d = latestCheckpoint.data as {
          message?: string;
          step?: number;
          total?: number;
        };
        const stepStr =
          d.step != null && d.total != null ? ` (${d.step}/${d.total})` : "";
        return (
          <div
            className="mt-2 text-[11px] text-teal-300 truncate"
            title={d.message ?? ""}
          >
            ◆ {d.message || "checkpoint"}
            {stepStr ? <span className="text-teal-500">{stepStr}</span> : null}
          </div>
        );
      })() : null}
      {att ? (
        <>
          <div className="flex items-center justify-between text-[10px] text-zinc-500 mt-2">
            <span>⏱ {formatRuntime(entry)}</span>
            <span>
              attempt {att.attempt_number}
              {att.terminal_reason ? ` · ${att.terminal_reason}` : ""}
            </span>
          </div>
          <div className="mt-1.5 h-1 bg-zinc-800 rounded overflow-hidden">
            <div
              className={`h-full transition-all ${
                att.state === "running" ? "bg-emerald-500" : "bg-zinc-500"
              }`}
              style={{ width: `${progress * 100}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[10px] text-zinc-500 mt-1">
            <span>
              {turns}/{turnsTotal} turns
            </span>
            <span>{cost > 0 ? `$${cost.toFixed(4)}` : ""}</span>
          </div>
        </>
      ) : (
        <div className="text-[10px] text-zinc-500 mt-2">
          {entry.queue_rank != null
            ? `queue rank: ${entry.queue_rank}`
            : "pending dispatch"}
        </div>
      )}
      {menuOpen && (
        <ContextMenu entry={entry} onClose={() => setMenuOpen(false)} />
      )}
    </div>
  );
}

export default memo(IssueCardInner);
