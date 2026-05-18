import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "../store";
import { useProjectStore, effectiveSubmitProjectId } from "../projectStore";
import { api } from "../api/client";
import { getStoredLLMConfig } from "../llmConfig";
import { chatCompletion } from "../llmClient";
import {
  decompositionPrompt,
  parseDecomposition,
  type SubTaskSpec,
} from "../chatToTasks";
import {
  appendConversation,
  listConversations,
  newConversationId,
  type ChatConversation,
} from "../chatHistory";
import type { ColumnKey } from "../api/types";

interface Props {
  open: boolean;
  onClose: () => void;
}

interface PreviewRow {
  task: string;
  depends_on: number[];
  included: boolean;
}

type Phase = "idle" | "asking-llm" | "preview" | "submitting";

interface LastBatch {
  parent_task_id: string;
  goal: string;
  subtask_count: number;
  /** Wall-clock the batch was created — used to suppress the panel for
   * old batches that the user dismissed and came back later. */
  created_at: number;
}

export default function ChatPanel({ open, onClose }: Props) {
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);
  const snapshot = useStore((s) => s.snapshot);
  const selectIssue = useStore((s) => s.selectIssue);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);

  const [goal, setGoal] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<PreviewRow[]>([]);
  const [rawLLMResponse, setRawLLMResponse] = useState<string>("");
  const [lastBatch, setLastBatch] = useState<LastBatch | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Per-project conversation history loaded from localStorage. Recomputed
  // when the project selection changes so switching projects shows the
  // right transcript list. `effectiveSubmitProjectId` resolves the
  // null-selection ("All projects") case to "default".
  const projectKey = effectiveSubmitProjectId(selectedProjectId);
  const [history, setHistory] = useState<ChatConversation[]>(() =>
    listConversations(projectKey),
  );

  // When the user switches project, refresh the history list and clear
  // any in-progress prompt / preview so the new project's panel doesn't
  // inherit a stale conversation from the previous one.
  useEffect(() => {
    setHistory(listConversations(projectKey));
    setGoal("");
    setPreview([]);
    setRawLLMResponse("");
    setPhase("idle");
    setLastBatch(null);
    abortRef.current?.abort();
    abortRef.current = null;
  }, [projectKey]);

  // Auto-clear preview when panel closes; abort any in-flight LLM call.
  useEffect(() => {
    if (!open) {
      abortRef.current?.abort();
      abortRef.current = null;
    }
  }, [open]);

  // Live status counts for the last-created batch, derived from the
  // store's snapshot. Updates automatically as the WS pushes new
  // state_snapshot messages while the children dispatch and finish.
  // `parent:<id>` label is the joining key (matches the kanban filter
  // logic in KanbanBoard).
  const batchCounts = useMemo(() => {
    if (!lastBatch || !snapshot) return null;
    const parentLabel = `parent:${lastBatch.parent_task_id}`;
    const out: Record<ColumnKey, number> = {
      pending: 0,
      claimed: 0,
      running: 0,
      retry_queued: 0,
      released: 0,
    };
    let blocked_for_human = 0;
    let failed = 0;
    let done = 0;
    for (const [col, entries] of Object.entries(snapshot.columns)) {
      for (const entry of entries) {
        if (!(entry.issue.labels ?? []).includes(parentLabel)) continue;
        out[col as ColumnKey] += 1;
        if (col === "released") {
          const tr = entry.attempt?.terminal_reason ?? "";
          if (tr === "needs_human") blocked_for_human += 1;
          else if (
            tr === "agent_finished" ||
            tr === "handoff" ||
            tr === "tracker_terminal"
          ) {
            done += 1;
          } else if (tr) {
            failed += 1;
          }
        }
      }
    }
    return { ...out, done, failed, blocked_for_human };
  }, [lastBatch, snapshot]);

  if (!open) return null;

  const handleSend = async () => {
    const trimmed = goal.trim();
    if (trimmed.length === 0) return;
    const cfg = getStoredLLMConfig();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setPhase("asking-llm");
    setPreview([]);
    setRawLLMResponse("");
    try {
      const text = await chatCompletion(decompositionPrompt(trimmed), cfg, {
        signal: ctrl.signal,
      });
      setRawLLMResponse(text);
      const { tasks, error } = parseDecomposition(text);
      if (error || tasks.length === 0) {
        setNotice({
          kind: "error",
          text: `decomposition failed: ${error ?? "no subtasks parsed"}`,
        });
        setPhase("idle");
        return;
      }
      setPreview(
        tasks.map((t: SubTaskSpec) => ({ ...t, included: true })),
      );
      setPhase("preview");
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setPhase("idle");
        return;
      }
      setNotice({ kind: "error", text: `LLM error: ${(err as Error).message}` });
      setPhase("idle");
    } finally {
      abortRef.current = null;
    }
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    setPhase("idle");
  };

  const togglePreviewRow = (idx: number) => {
    setPreview((prev) =>
      prev.map((r, i) => (i === idx ? { ...r, included: !r.included } : r)),
    );
  };

  const handleCreate = async () => {
    // Build the subtasks payload from included rows only. Re-index
    // depends_on against the new (possibly shorter) list — drop any
    // dep that points at an excluded row.
    const includedIdx = preview
      .map((r, i) => (r.included ? i : -1))
      .filter((i) => i >= 0);
    const idxMap = new Map<number, number>();
    includedIdx.forEach((origIdx, newIdx) => idxMap.set(origIdx, newIdx));
    const subtasks = includedIdx.map((origIdx) => {
      const row = preview[origIdx];
      const remappedDeps = row.depends_on
        .map((d) => idxMap.get(d))
        .filter((d): d is number => typeof d === "number");
      return { task: row.task, depends_on: remappedDeps };
    });

    if (subtasks.length === 0) {
      setNotice({ kind: "error", text: "no subtasks selected" });
      return;
    }

    setPhase("submitting");
    try {
      const r = await api.submitCodingTask({
        task: goal.trim(),
        subtasks,
        project_id: projectKey,
      });
      setNotice({
        kind: "info",
        text: `Created ${subtasks.length} subtask(s) under ${r.task_id}`,
      });
      // Persist this conversation under the active project. Switching
      // project later restores its own list; this entry only shows up
      // when the user is looking at *this* project.
      const updated = appendConversation(projectKey, {
        id: newConversationId(),
        created_at: new Date().toISOString(),
        goal: goal.trim(),
        parent_task_id: r.task_id,
        subtasks_created: subtasks.length,
      });
      setHistory(updated);
      // Latch this batch so the panel shows a live status footer
      // (counts derived from useStore.snapshot via the project label
      // `parent:<id>`). User can dismiss with the Done button.
      setLastBatch({
        parent_task_id: r.task_id,
        goal: goal.trim(),
        subtask_count: subtasks.length,
        created_at: Date.now(),
      });
      void refresh();
      setGoal("");
      setPreview([]);
      setRawLLMResponse("");
      setPhase("idle");
    } catch (err) {
      setNotice({ kind: "error", text: `submit failed: ${(err as Error).message}` });
      setPhase("preview");
    }
  };

  const selectedCount = preview.filter((r) => r.included).length;

  return (
    <aside
      role="dialog"
      aria-label="Chat to generate kanban cards"
      className="fixed bottom-4 right-4 z-40 w-[400px] max-w-[95vw] h-[640px] max-h-[80vh] bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-2 border-b border-zinc-800">
        <h2 className="text-sm font-semibold">💬 Decompose into cards</h2>
        <button
          onClick={onClose}
          className="text-zinc-400 hover:text-zinc-100 text-lg leading-none px-1"
          aria-label="Close chat panel"
        >
          ×
        </button>
      </header>

      <div className="flex-1 min-h-0 overflow-auto px-4 py-3 text-xs">
        {phase === "idle" && preview.length === 0 ? (
          <>
            <p className="text-zinc-500">
              Describe a coding goal and I&apos;ll break it into subtasks via
              your configured LLM (see the 🔌 LLM button to change endpoint).
              The preview lets you uncheck anything before creating cards.
            </p>
            {history.length > 0 ? (
              <section className="mt-3" aria-label="Recent conversations">
                <header className="flex items-center justify-between text-zinc-500 uppercase tracking-wider text-[10px] mb-1">
                  <span>Recent in this project</span>
                  <span>{history.length}</span>
                </header>
                <ul className="space-y-1">
                  {history.slice(0, 8).map((c) => (
                    <li key={c.id}>
                      <button
                        onClick={() => setGoal(c.goal)}
                        className="w-full text-left p-2 rounded border border-zinc-800 bg-zinc-800/40 hover:bg-zinc-800 transition"
                        title="Click to prefill the input with this goal"
                      >
                        <div className="text-zinc-200 truncate">{c.goal}</div>
                        <div className="text-zinc-500 text-[10px] font-mono truncate">
                          {c.parent_task_id} · {c.subtasks_created} subtask
                          {c.subtasks_created === 1 ? "" : "s"}
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </>
        ) : phase === "asking-llm" ? (
          <p className="text-zinc-400">
            Asking the LLM to decompose… (Cancel to abort)
          </p>
        ) : phase === "preview" || phase === "submitting" ? (
          <div className="space-y-2">
            <p className="text-zinc-400">
              {selectedCount} of {preview.length} subtask(s) will be created
              under a new parent task.
            </p>
            <ul className="space-y-1.5">
              {preview.map((row, i) => (
                <li
                  key={i}
                  className="flex items-start gap-2 border border-zinc-800 rounded p-2 bg-zinc-800/40"
                >
                  <input
                    type="checkbox"
                    checked={row.included}
                    onChange={() => togglePreviewRow(i)}
                    className="mt-0.5"
                    aria-label={`Include subtask ${i + 1}`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-zinc-200">{row.task}</div>
                    {row.depends_on.length > 0 ? (
                      <div className="text-zinc-500 text-[10px] mt-0.5">
                        depends on:{" "}
                        {row.depends_on.map((d) => `#${d + 1}`).join(", ")}
                      </div>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
            {rawLLMResponse ? (
              <details className="text-zinc-500">
                <summary className="cursor-pointer">raw LLM response</summary>
                <pre className="mt-1 p-2 bg-zinc-950 border border-zinc-800 rounded overflow-auto whitespace-pre-wrap text-[10px]">
                  {rawLLMResponse}
                </pre>
              </details>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Phase D2b / E observability — live counts for the last-created
          batch. Reads the store's snapshot so as agents dispatch + finish
          the numbers update in real time without any extra plumbing.
          Click a row to drill into the parent issue's drawer. */}
      {lastBatch && batchCounts && phase === "idle" ? (
        <div className="border-t border-zinc-800 px-4 py-2 bg-zinc-800/30 text-xs">
          <div className="flex items-center justify-between gap-2">
            <div className="text-zinc-400 truncate" title={lastBatch.goal}>
              <span className="text-zinc-200">▸</span> Last batch ·{" "}
              <span className="text-zinc-200">
                {lastBatch.subtask_count} task
                {lastBatch.subtask_count === 1 ? "" : "s"}
              </span>
            </div>
            <button
              onClick={() => setLastBatch(null)}
              className="text-zinc-500 hover:text-zinc-200"
              title="Dismiss live status footer"
              aria-label="Dismiss last-batch status"
            >
              ×
            </button>
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] mt-1 font-mono">
            <span className="text-zinc-400">
              pending <span className="text-zinc-200">{batchCounts.pending}</span>
            </span>
            <span className="text-zinc-400">
              running <span className="text-emerald-300">{batchCounts.running + batchCounts.claimed}</span>
            </span>
            <span className="text-zinc-400">
              done <span className="text-emerald-300">{batchCounts.done}</span>
            </span>
            {batchCounts.failed > 0 ? (
              <span className="text-zinc-400">
                failed <span className="text-rose-300">{batchCounts.failed}</span>
              </span>
            ) : null}
            {batchCounts.blocked_for_human > 0 ? (
              <span className="text-zinc-400">
                blocked-for-human{" "}
                <span className="text-amber-300">
                  {batchCounts.blocked_for_human}
                </span>
              </span>
            ) : null}
          </div>
          <div className="flex items-center gap-2 mt-1.5 text-[10px]">
            <button
              onClick={() => {
                selectIssue(lastBatch.parent_task_id);
                onClose();
              }}
              className="text-emerald-300 hover:underline"
              title="Open the parent task on the kanban"
            >
              View parent on kanban →
            </button>
            <span className="text-zinc-600">·</span>
            <span className="text-zinc-500 font-mono truncate">
              {lastBatch.parent_task_id}
            </span>
          </div>
        </div>
      ) : null}

      <div className="border-t border-zinc-800 px-3 py-2">
        <textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="e.g. Build a TODO API with FastAPI + integration tests"
          rows={2}
          disabled={phase === "asking-llm" || phase === "submitting"}
          className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 resize-none"
        />
        <div className="flex items-center justify-end gap-2 mt-2">
          {phase === "asking-llm" ? (
            <button
              onClick={handleCancel}
              className="px-3 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
            >
              Cancel
            </button>
          ) : phase === "preview" ? (
            <>
              <button
                onClick={() => {
                  setPreview([]);
                  setPhase("idle");
                }}
                className="px-3 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
              >
                Discard
              </button>
              <button
                onClick={handleCreate}
                disabled={selectedCount === 0}
                className="px-3 py-1 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Create {selectedCount} card{selectedCount === 1 ? "" : "s"}
              </button>
            </>
          ) : (
            <button
              onClick={handleSend}
              disabled={
                goal.trim().length === 0 || (phase as Phase) === "submitting"
              }
              className="px-3 py-1 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {phase === "submitting" ? "Submitting…" : "Send"}
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}
