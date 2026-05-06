import { useState } from "react";
import { useStore } from "../store";

export default function ActivityFeed() {
  const activity = useStore((s) => s.activity);
  const select = useStore((s) => s.selectIssue);
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div
      className={`fixed right-4 bottom-4 w-80 ${
        collapsed ? "h-9" : "h-72"
      } bg-zinc-900 border border-zinc-800 rounded-lg shadow-xl flex flex-col overflow-hidden z-30 transition-all`}
    >
      <button
        onClick={() => setCollapsed((v) => !v)}
        className="flex items-center justify-between px-3 py-2 border-b border-zinc-800 text-xs uppercase tracking-wider text-zinc-300 hover:bg-zinc-800/40"
      >
        <span>Activity ({activity.length})</span>
        <span>{collapsed ? "▲" : "▼"}</span>
      </button>
      {!collapsed && (
        <div className="flex-1 min-h-0 overflow-auto text-xs">
          {activity.length === 0 ? (
            <div className="px-3 py-4 text-zinc-500 italic text-center">
              waiting for events…
            </div>
          ) : (
            activity.map((a) => (
              <button
                key={a.id}
                onClick={() => select(a.issue_id)}
                className="w-full text-left px-3 py-1.5 hover:bg-zinc-800/60 border-b border-zinc-800/50 flex items-baseline gap-2"
              >
                <span className="text-zinc-500 font-mono shrink-0">
                  {a.timestamp.slice(11, 19)}
                </span>
                <span className="text-zinc-400 font-mono shrink-0">
                  {a.issue_id.slice(-8)}
                </span>
                <span className="text-zinc-200 truncate">{a.summary}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
