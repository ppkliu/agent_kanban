import { useState } from "react";
import { useStore } from "../../store";
import { api } from "../../api/client";
import type { IssueDetail } from "../../api/types";

export default function HintsTab({
  detail,
  onChanged,
}: {
  detail: IssueDetail;
  onChanged: () => void;
}) {
  const setNotice = useStore((s) => s.setNotice);
  const [author, setAuthor] = useState(localStorage.getItem("symphony.author") ?? "");
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const pending = detail.hints.filter((h) => !h.consumed);
  const consumed = detail.hints.filter((h) => h.consumed);

  async function submit() {
    if (!author.trim() || !content.trim()) return;
    setSubmitting(true);
    try {
      await api.addHint(detail.issue.id, author.trim(), content.trim());
      localStorage.setItem("symphony.author", author.trim());
      setContent("");
      setNotice({ kind: "info", text: "Hint added — will inject on next attempt" });
      onChanged();
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col h-full overflow-auto p-6 gap-6">
      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Add hint
        </h3>
        <p className="text-xs text-zinc-500 mb-3">
          Hints are appended to the prompt on the next attempt. Symphony's
          tracker stays read-only — the agent must turn this into a comment or
          PR action itself.
        </p>
        <div className="space-y-2">
          <input
            placeholder="your name"
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
            className="w-48 bg-zinc-900 border border-zinc-800 rounded px-3 py-1.5 text-sm placeholder:text-zinc-500"
          />
          <textarea
            placeholder="e.g. previous PR #88 used useFormState; performance was bad — switch to useReducer"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={4}
            className="w-full bg-zinc-900 border border-zinc-800 rounded px-3 py-2 text-sm placeholder:text-zinc-500 resize-vertical"
          />
          <div className="flex justify-end">
            <button
              disabled={submitting || !author.trim() || !content.trim()}
              onClick={submit}
              className="px-4 py-1.5 bg-emerald-700 hover:bg-emerald-600 disabled:bg-zinc-800 disabled:text-zinc-500 text-white text-sm rounded"
            >
              Add hint
            </button>
          </div>
        </div>
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Pending injection ({pending.length})
        </h3>
        {pending.length === 0 ? (
          <div className="text-xs text-zinc-500 italic">
            no hints will be injected on next attempt
          </div>
        ) : (
          <ul className="space-y-2">
            {pending.map((h) => (
              <li
                key={h.id}
                className="bg-emerald-900/20 border border-emerald-800/50 rounded p-3 text-sm"
              >
                <div className="text-[11px] text-emerald-400/80 mb-1 flex justify-between">
                  <span>{h.author}</span>
                  <span>{h.created_at.slice(0, 19).replace("T", " ")}</span>
                </div>
                <p className="whitespace-pre-wrap text-zinc-200">{h.content}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3 className="text-xs uppercase tracking-wider text-zinc-400 mb-2">
          Consumed history ({consumed.length})
        </h3>
        {consumed.length === 0 ? (
          <div className="text-xs text-zinc-500 italic">
            no hints have been consumed yet
          </div>
        ) : (
          <ul className="space-y-2">
            {consumed.map((h) => (
              <li
                key={h.id}
                className="bg-zinc-900/40 border border-zinc-800 rounded p-3 text-sm opacity-70"
              >
                <div className="text-[11px] text-zinc-500 mb-1 flex justify-between">
                  <span>{h.author}</span>
                  <span>
                    consumed{" "}
                    {h.consumed_attempt
                      ? `at attempt #${h.consumed_attempt}`
                      : ""}
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-zinc-300">{h.content}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
