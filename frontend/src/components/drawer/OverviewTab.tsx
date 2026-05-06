import { useStore } from "../../store";
import { api } from "../../api/client";
import type { IssueDetail } from "../../api/types";

export default function OverviewTab({
  detail,
  onMutated,
}: {
  detail: IssueDetail;
  onMutated: () => void;
}) {
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);
  const id = detail.issue.id;
  const att = detail.current_attempt;

  async function run(label: string, fn: () => Promise<unknown>) {
    try {
      await fn();
      setNotice({ kind: "info", text: `${label} ok` });
      onMutated();
      void refresh();
    } catch (e) {
      setNotice({ kind: "error", text: `${label}: ${(e as Error).message}` });
    }
  }

  return (
    <div className="overflow-auto h-full p-6 space-y-6">
      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Description
        </h3>
        <pre className="text-xs whitespace-pre-wrap leading-relaxed text-zinc-300 bg-zinc-900/40 border border-zinc-800 rounded p-3">
          {detail.issue.description?.trim() || "(no description)"}
        </pre>
      </section>

      {detail.issue.labels && detail.issue.labels.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
            Labels
          </h3>
          <div className="flex flex-wrap gap-1">
            {detail.issue.labels.map((l) => (
              <span
                key={l}
                className="text-[11px] bg-zinc-800 border border-zinc-700 rounded px-2 py-0.5"
              >
                {l}
              </span>
            ))}
          </div>
        </section>
      )}

      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Run history
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-zinc-500 border-b border-zinc-800">
              <tr>
                <th className="text-left py-1 pr-3">#</th>
                <th className="text-left py-1 pr-3">state</th>
                <th className="text-left py-1 pr-3">terminal_reason</th>
                <th className="text-left py-1 pr-3">turns</th>
                <th className="text-left py-1 pr-3">cost</th>
                <th className="text-left py-1 pr-3">started</th>
                <th className="text-left py-1 pr-3">ended</th>
              </tr>
            </thead>
            <tbody className="text-zinc-300">
              {detail.all_attempts.map((row) => (
                <tr key={row.attempt_number} className="border-b border-zinc-800/50">
                  <td className="py-1 pr-3 font-mono">{row.attempt_number}</td>
                  <td className="py-1 pr-3">{row.state}</td>
                  <td className="py-1 pr-3">{row.terminal_reason ?? "—"}</td>
                  <td className="py-1 pr-3">{row.turns_consumed}</td>
                  <td className="py-1 pr-3">${row.cost_usd?.toFixed?.(4) ?? "0.0000"}</td>
                  <td className="py-1 pr-3 text-zinc-500">
                    {row.started_at?.replace("T", " ").slice(0, 19) ?? "—"}
                  </td>
                  <td className="py-1 pr-3 text-zinc-500">
                    {row.ended_at?.replace("T", " ").slice(0, 19) ?? "—"}
                  </td>
                </tr>
              ))}
              {att && att.state !== "released" && (
                <tr className="bg-emerald-900/10">
                  <td className="py-1 pr-3 font-mono">{att.attempt_number}</td>
                  <td className="py-1 pr-3 text-emerald-300">
                    {att.state} (live)
                  </td>
                  <td className="py-1 pr-3">—</td>
                  <td className="py-1 pr-3">{att.turns_consumed}</td>
                  <td className="py-1 pr-3">${att.cost_usd.toFixed(4)}</td>
                  <td className="py-1 pr-3 text-zinc-500">
                    {att.started_at?.replace("T", " ").slice(0, 19) ?? "—"}
                  </td>
                  <td className="py-1 pr-3 text-zinc-500">—</td>
                </tr>
              )}
              {detail.all_attempts.length === 0 && !att && (
                <tr>
                  <td colSpan={7} className="py-3 text-center text-zinc-500">
                    no attempts yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Actions
        </h3>
        <div className="flex flex-wrap gap-2">
          <button
            disabled={!att || (att.state !== "claimed" && att.state !== "running")}
            onClick={() => run("pause", () => api.pause(id))}
            className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-40"
          >
            ⏸ Pause
          </button>
          <button
            disabled={!att?.paused_until}
            onClick={() => run("resume", () => api.resume(id))}
            className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-40"
          >
            ▶ Resume
          </button>
          <button
            disabled={!att || (att.state !== "claimed" && att.state !== "running")}
            onClick={() => {
              if (confirm(`Abort ${detail.issue.identifier}?`))
                void run("abort", () => api.abort(id, "operator aborted"));
            }}
            className="px-3 py-1.5 text-xs rounded bg-rose-900/40 hover:bg-rose-800/40 border border-rose-700 disabled:opacity-40"
          >
            ✕ Abort
          </button>
          <button
            disabled={att?.state !== "released"}
            onClick={() => run("retry", () => api.retry(id))}
            className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-40"
          >
            ↻ Force retry
          </button>
        </div>
      </section>
    </div>
  );
}
