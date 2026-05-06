import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { EventRecord } from "../../api/types";

const SPEEDS = [0.5, 1, 2, 4, 8] as const;

export default function ReplayTab({ issueId }: { issueId: string }) {
  const [attempts, setAttempts] = useState<number[]>([]);
  const [attemptN, setAttemptN] = useState<number | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    api
      .issueAttempts(issueId)
      .then((r) => {
        const ns = r.history.map((h) => h.attempt_number);
        if (r.current && !ns.includes(r.current.attempt_number))
          ns.push(r.current.attempt_number);
        setAttempts(ns);
        if (ns.length && attemptN == null) setAttemptN(ns[0]);
      })
      .catch(() => setAttempts([]));
  }, [issueId, attemptN]);

  useEffect(() => {
    if (attemptN == null) return;
    api
      .issueEvents(issueId, { attempt_number: attemptN, limit: 5000 })
      .then((r) => {
        setEvents(r.events);
        setPos(0);
      })
      .catch(() => setEvents([]));
  }, [issueId, attemptN]);

  useEffect(() => {
    if (!playing) return;
    if (pos >= events.length - 1) {
      setPlaying(false);
      return;
    }
    const cur = events[pos];
    const nxt = events[pos + 1];
    const delta = nxt && cur
      ? new Date(nxt.timestamp).getTime() - new Date(cur.timestamp).getTime()
      : 200;
    const wait = Math.max(50, Math.min(2000, delta / speed));
    timerRef.current = window.setTimeout(() => setPos((p) => p + 1), wait);
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [playing, pos, events, speed]);

  const visible = useMemo(
    () => events.slice(0, pos + 1),
    [events, pos],
  );

  if (attempts.length === 0) {
    return (
      <div className="p-6 text-zinc-500 text-sm">
        No persisted attempts to replay yet.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-900/30 text-xs flex items-center gap-2">
        <select
          value={attemptN ?? ""}
          onChange={(e) => setAttemptN(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1"
        >
          {attempts.map((n) => (
            <option key={n} value={n}>
              attempt #{n}
            </option>
          ))}
        </select>

        <button
          onClick={() => setPlaying((v) => !v)}
          className="px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white"
        >
          {playing ? "⏸" : "▶"}
        </button>
        <button
          onClick={() => setPos(0)}
          className="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
        >
          ⏪
        </button>
        <select
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1"
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>

        <input
          type="range"
          min={0}
          max={Math.max(0, events.length - 1)}
          value={pos}
          onChange={(e) => setPos(Number(e.target.value))}
          className="flex-1"
        />
        <span className="text-zinc-400 font-mono w-20 text-right">
          {pos + 1} / {events.length}
        </span>
      </div>

      <div className="flex-1 min-h-0 overflow-auto px-4 py-3 font-mono text-xs space-y-0.5">
        {visible.map((ev, i) => (
          <div key={i} className="flex items-baseline gap-2">
            <span className="text-zinc-500">{ev.timestamp.slice(11, 19)}</span>
            <span className="text-zinc-300">{ev.kind}</span>
            <span className="text-zinc-400 truncate">
              {ev.kind === "tool_call"
                ? (ev.data as { tool?: string }).tool
                : ev.kind === "message_delta"
                  ? (ev.data as { text?: string }).text?.slice(0, 100)
                  : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
