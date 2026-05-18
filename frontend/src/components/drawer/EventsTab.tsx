import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "../../store";
import { api } from "../../api/client";
import type { EventRecord } from "../../api/types";

interface Filters {
  message_delta: boolean;
  thinking: boolean;
  tool_call: boolean;
  errors: boolean;
  checkpoint: boolean;
}

const DEFAULT_FILTERS: Filters = {
  message_delta: false,
  thinking: false,
  tool_call: true,
  errors: true,
  checkpoint: true,
};

function passes(ev: EventRecord, f: Filters): boolean {
  if (ev.kind === "message_delta" && !f.message_delta) return false;
  if (ev.kind === "thinking" && !f.thinking) return false;
  if (
    (ev.kind === "tool_call" || ev.kind === "tool_result" ||
      ev.kind === "item_started" || ev.kind === "item_completed") &&
    !f.tool_call
  )
    return false;
  if (ev.kind === "error" && !f.errors) return false;
  if (ev.kind === "checkpoint" && !f.checkpoint) return false;
  return true;
}

function eventColor(kind: string): string {
  if (kind === "turn_started" || kind === "turn_completed") return "text-emerald-400";
  if (kind === "tool_call") return "text-amber-400";
  if (kind === "tool_result") return "text-violet-400";
  if (kind === "message_delta") return "text-blue-400";
  if (kind === "checkpoint") return "text-teal-400";
  if (kind === "error") return "text-rose-400";
  return "text-zinc-400";
}

function eventIcon(kind: string): string {
  if (kind === "turn_started") return "▶";
  if (kind === "turn_completed") return "✓";
  if (kind === "tool_call") return "🔧";
  if (kind === "tool_result") return "↩";
  if (kind === "message_delta") return "💬";
  if (kind === "checkpoint") return "◆";
  if (kind === "error") return "⚠";
  if (kind === "done") return "✔";
  return "·";
}

export default function EventsTab({ issueId }: { issueId: string }) {
  const live = useStore((s) => s.recentEvents[issueId] ?? []);
  const [hist, setHist] = useState<EventRecord[]>([]);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [autoscroll, setAutoscroll] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .issueEvents(issueId, { limit: 2000 })
      .then((r) => setHist(r.events))
      .catch(() => setHist([]));
  }, [issueId]);

  // Merge persisted history with live tail by timestamp.
  const events = useMemo(() => {
    const seen = new Set<string>();
    const out: EventRecord[] = [];
    for (const e of [...hist, ...live]) {
      const key = `${e.timestamp}::${e.kind}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(e);
    }
    out.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    return out;
  }, [hist, live]);

  useEffect(() => {
    if (!autoscroll) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events, autoscroll]);

  function toggleExpand(idx: number) {
    setExpanded((s) => {
      const ns = new Set(s);
      if (ns.has(idx)) ns.delete(idx);
      else ns.add(idx);
      return ns;
    });
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 bg-zinc-900/30 text-xs">
        {(["message_delta", "thinking", "tool_call", "errors", "checkpoint"] as const).map(
          (k) => (
            <label key={k} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={filters[k]}
                onChange={(e) =>
                  setFilters((f) => ({ ...f, [k]: e.target.checked }))
                }
              />
              <span className="text-zinc-400">{k}</span>
            </label>
          ),
        )}
        <label className="flex items-center gap-1.5 ml-auto">
          <input
            type="checkbox"
            checked={autoscroll}
            onChange={(e) => setAutoscroll(e.target.checked)}
          />
          <span className="text-zinc-400">auto-scroll</span>
        </label>
      </div>

      <div
        ref={containerRef}
        className="flex-1 min-h-0 overflow-auto px-4 py-3 font-mono text-xs space-y-0.5"
      >
        {events.filter((e) => passes(e, filters)).map((ev, idx) => {
          const isOpen = expanded.has(idx);
          const expandable = ev.kind === "tool_call" || ev.kind === "tool_result";
          return (
            <div key={idx}>
              <div
                className={`flex items-baseline gap-2 ${
                  expandable ? "cursor-pointer hover:bg-zinc-800/40" : ""
                } px-1 rounded`}
                onClick={() => expandable && toggleExpand(idx)}
              >
                <span className="text-zinc-500">
                  {ev.timestamp.slice(11, 19)}
                </span>
                <span className={`${eventColor(ev.kind)} w-4 text-center`}>
                  {eventIcon(ev.kind)}
                </span>
                <span className={eventColor(ev.kind)}>{ev.kind}</span>
                {ev.kind === "tool_call" && (
                  <span className="text-zinc-300">
                    {(ev.data as { tool?: string }).tool ?? "?"}
                  </span>
                )}
                {ev.kind === "tool_result" &&
                  (ev.data as { is_error?: boolean }).is_error && (
                    <span className="text-rose-400">error</span>
                  )}
                {ev.kind === "message_delta" && (
                  <span className="text-zinc-300 truncate">
                    {(ev.data as { text?: string }).text?.slice(0, 200)}
                  </span>
                )}
                {ev.kind === "checkpoint" && (() => {
                  const d = ev.data as { message?: string; step?: number; total?: number };
                  const stepStr = d.step != null && d.total != null
                    ? ` (${d.step}/${d.total})`
                    : "";
                  return (
                    <span className="text-teal-300 truncate">
                      {d.message ?? ""}
                      {stepStr ? <span className="text-teal-500">{stepStr}</span> : null}
                    </span>
                  );
                })()}
                {ev.kind === "error" && (
                  <span className="text-rose-300 truncate">
                    {(ev.data as { message?: string }).message}
                  </span>
                )}
              </div>
              {expandable && isOpen && (
                <pre className="ml-12 my-1 p-2 text-[11px] bg-zinc-900/60 border border-zinc-800 rounded overflow-x-auto">
                  {JSON.stringify(ev.data, null, 2)}
                </pre>
              )}
            </div>
          );
        })}
        {events.length === 0 && (
          <div className="text-zinc-500 italic py-8 text-center">
            No events yet
          </div>
        )}
      </div>
    </div>
  );
}
