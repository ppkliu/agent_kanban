import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { api } from "../api/client";
import { getStoredLLMConfig } from "../llmConfig";
import { chatCompletion } from "../llmClient";
import {
  decompositionPrompt,
  parseDecomposition,
  type SubTaskSpec,
} from "../chatToTasks";

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

export default function ChatPanel({ open, onClose }: Props) {
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);

  const [goal, setGoal] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [preview, setPreview] = useState<PreviewRow[]>([]);
  const [rawLLMResponse, setRawLLMResponse] = useState<string>("");
  const abortRef = useRef<AbortController | null>(null);

  // Auto-clear preview when panel closes; abort any in-flight LLM call.
  useEffect(() => {
    if (!open) {
      abortRef.current?.abort();
      abortRef.current = null;
    }
  }, [open]);

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
      });
      setNotice({
        kind: "info",
        text: `Created ${subtasks.length} subtask(s) under ${r.task_id}`,
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
          <p className="text-zinc-500">
            Describe a coding goal and I&apos;ll break it into subtasks via your
            configured LLM (see the 🔌 LLM button to change endpoint). The
            preview lets you uncheck anything before creating cards.
          </p>
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
