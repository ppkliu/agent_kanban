import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../store";

/** Backend runner config modal — operator surface that mirrors WORKFLOW.md's
 * `runner:` block. Mutations call PATCH /api/v1/config which rebuilds
 * orch.runner in-memory. They are NOT persisted to WORKFLOW.md — a
 * container restart reverts to whatever the file says. */

type RunnerKind = "echo" | "opencode" | "anthropic_api" | "claude_cli";

interface FormState {
  runner_kind: RunnerKind;
  runner_model: string;
  runner_provider: string;
  runner_base_url: string;
  runner_api_key: string;
}

const KIND_LABELS: Record<RunnerKind, string> = {
  echo: "Echo (test only, no LLM)",
  opencode: "OpenCode CLI (recommended — vLLM / Ollama / cloud via LiteLLM)",
  anthropic_api: "Anthropic API (direct /v1/messages)",
  claude_cli: "Claude Code CLI (wraps `claude -p`)",
};

const KIND_NEEDS_PROVIDER: Record<RunnerKind, boolean> = {
  echo: false,
  opencode: true,
  anthropic_api: false,
  claude_cli: false,
};

const KIND_NEEDS_MODEL: Record<RunnerKind, boolean> = {
  echo: false,
  opencode: true,
  anthropic_api: true,
  claude_cli: true,
};

const KIND_NEEDS_ENDPOINT: Record<RunnerKind, boolean> = {
  echo: false,
  opencode: true,
  anthropic_api: true,
  claude_cli: false,
};

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function BackendRunnerSettings({ open, onClose }: Props) {
  const cfg = useStore((s) => s.snapshot?.config);
  const setNotice = useStore((s) => s.setNotice);

  const [form, setForm] = useState<FormState>(() => ({
    runner_kind: (cfg?.runner_kind as RunnerKind) ?? "echo",
    runner_model: cfg?.runner_model ?? "",
    runner_provider: cfg?.runner_provider ?? "vllm",
    runner_base_url: "",
    runner_api_key: "",
  }));
  const [saving, setSaving] = useState(false);

  // Sync the form with snapshot when the modal opens (or when the snapshot
  // updates via a config_changed push).
  useEffect(() => {
    if (!open) return;
    setForm((prev) => ({
      ...prev,
      runner_kind: (cfg?.runner_kind as RunnerKind) ?? prev.runner_kind,
      runner_model: cfg?.runner_model ?? prev.runner_model,
      runner_provider: cfg?.runner_provider ?? prev.runner_provider,
    }));
  }, [open, cfg?.runner_kind, cfg?.runner_model, cfg?.runner_provider]);

  if (!open) return null;

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleSave = async () => {
    setSaving(true);
    try {
      const patch: Parameters<typeof api.patchConfig>[0] = {
        runner_kind: form.runner_kind,
      };
      if (KIND_NEEDS_MODEL[form.runner_kind] && form.runner_model)
        patch.runner_model = form.runner_model;
      if (KIND_NEEDS_PROVIDER[form.runner_kind] && form.runner_provider)
        patch.runner_provider = form.runner_provider;
      if (KIND_NEEDS_ENDPOINT[form.runner_kind] && form.runner_base_url)
        patch.runner_base_url = form.runner_base_url;
      if (form.runner_api_key) patch.runner_api_key = form.runner_api_key;

      await api.patchConfig(patch);
      setNotice({
        kind: "info",
        text: `Backend runner → ${form.runner_kind}${
          form.runner_model ? ` · ${form.runner_model}` : ""
        }`,
      });
      onClose();
    } catch (err) {
      setNotice({
        kind: "error",
        text: `runner patch failed: ${(err as Error).message}`,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
        aria-label="Close backend runner settings backdrop"
      />
      <div
        role="dialog"
        aria-label="Backend runner settings"
        className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[520px] max-w-[95vw] bg-zinc-900 border border-zinc-700 rounded-md shadow-2xl"
      >
        <header className="flex items-center justify-between px-5 py-3 border-b border-zinc-800">
          <h2 className="text-sm font-semibold">
            Backend runner (executes coding tasks)
          </h2>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 text-lg leading-none px-1"
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <div className="px-5 py-4 space-y-4 text-xs">
          <p className="text-zinc-400">
            What Symphony spawns per issue to do the actual coding work.
            Mutations apply in memory only; restart the container to fall
            back to <code>WORKFLOW.md</code>. The browser chat panel (
            <span className="text-zinc-200">🔌 LLM</span>) is a separate
            config used only by the chat-to-cards decomposition flow.
          </p>

          <label className="block">
            <span className="block text-zinc-300 mb-1">Runner kind</span>
            <select
              value={form.runner_kind}
              onChange={(e) => update("runner_kind", e.target.value as RunnerKind)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100"
            >
              {(Object.keys(KIND_LABELS) as RunnerKind[]).map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </label>

          {KIND_NEEDS_PROVIDER[form.runner_kind] ? (
            <label className="block">
              <span className="block text-zinc-300 mb-1">
                Provider (LiteLLM id)
              </span>
              <input
                type="text"
                value={form.runner_provider}
                onChange={(e) => update("runner_provider", e.target.value)}
                placeholder="vllm / ollama / openai / anthropic / ..."
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
              />
            </label>
          ) : null}

          {KIND_NEEDS_MODEL[form.runner_kind] ? (
            <label className="block">
              <span className="block text-zinc-300 mb-1">Model</span>
              <input
                type="text"
                value={form.runner_model}
                onChange={(e) => update("runner_model", e.target.value)}
                placeholder="e.g. qwen3.6-27b-fp8"
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
              />
            </label>
          ) : null}

          {KIND_NEEDS_ENDPOINT[form.runner_kind] ? (
            <label className="block">
              <span className="block text-zinc-300 mb-1">
                Base URL{" "}
                <span className="text-zinc-500">
                  (sets <code>OPENCODE_BASE_URL</code>)
                </span>
              </span>
              <input
                type="text"
                value={form.runner_base_url}
                onChange={(e) => update("runner_base_url", e.target.value)}
                placeholder="leave empty to keep current env value"
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
              />
            </label>
          ) : null}

          <label className="block">
            <span className="block text-zinc-300 mb-1">
              API key{" "}
              <span className="text-zinc-500">
                (sets <code>OPENCODE_API_KEY</code> /{" "}
                <code>ANTHROPIC_API_KEY</code>)
              </span>
            </span>
            <input
              type="password"
              value={form.runner_api_key}
              onChange={(e) => update("runner_api_key", e.target.value)}
              placeholder="leave empty to keep current env value"
              autoComplete="off"
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
            />
          </label>

          <div className="text-zinc-500 text-[11px] pt-2 border-t border-zinc-800">
            In-flight workers keep their captured runner reference and finish
            on the old runner; new dispatches use the new one. To persist
            across restarts, edit <code>WORKFLOW.md</code> (📝 Workflow
            button) instead — the file is the source of truth on container
            boot.
          </div>
        </div>

        <footer className="flex items-center justify-end gap-2 px-5 py-3 border-t border-zinc-800">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-3 py-1.5 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-700 disabled:opacity-50"
          >
            {saving ? "Applying…" : "Apply"}
          </button>
        </footer>
      </div>
    </>
  );
}
