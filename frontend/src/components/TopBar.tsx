import { useEffect, useState } from "react";
import { useStore } from "../store";
import { api, setApiKey } from "../api/client";
import { applyTheme, getStoredTheme, toggleTheme, type Theme } from "../theme";
import { getStoredLLMConfig, type LLMConfig } from "../llmConfig";
import AgentExplainer from "./AgentExplainer";
import LLMSettings from "./LLMSettings";
import ChatPanel from "./ChatPanel";
import BackendRunnerSettings from "./BackendRunnerSettings";
import ProjectSelector from "./ProjectSelector";

export default function TopBar() {
  const status = useStore((s) => s.status);
  const snapshot = useStore((s) => s.snapshot);
  const toggleWf = useStore((s) => s.toggleWorkflowEditor);
  const wfOpen = useStore((s) => s.workflowEditorOpen);
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);

  const [editingMax, setEditingMax] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => getStoredTheme());
  const [explainerOpen, setExplainerOpen] = useState(false);
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [llmConfig, setLlmConfig] = useState<LLMConfig>(() =>
    getStoredLLMConfig(),
  );
  const [runnerSettingsOpen, setRunnerSettingsOpen] = useState(false);

  // Keep <html data-theme="…"> in sync if `theme` is changed via the switcher.
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const cfg = snapshot?.config;
  const totals = snapshot?.totals;

  return (
    <header className="flex items-center gap-4 px-6 py-3 border-b border-zinc-800 bg-zinc-900/40">
      <div className="flex items-center gap-2">
        <div
          className={`w-2.5 h-2.5 rounded-full ${
            status === "open"
              ? "bg-emerald-400 animate-pulse"
              : status === "connecting"
                ? "bg-amber-400 animate-pulse"
                : "bg-rose-500"
          }`}
          title={`WebSocket: ${status}`}
        />
        <span className="font-semibold tracking-tight">Symphony</span>
      </div>

      <ProjectSelector />

      <div className="text-xs text-zinc-400 flex items-center gap-3">
        <span>
          tracker:{" "}
          <span className="text-zinc-200">{cfg?.tracker_kind ?? "—"}</span>
          {cfg?.tracker_repo ? (
            <span className="text-zinc-500"> · {cfg.tracker_repo}</span>
          ) : null}
        </span>
        <span
          className="relative inline-flex items-center gap-1"
          title="Backend runner — what the orchestrator dispatches per issue (from WORKFLOW.md). Click 🔧 Runner to change at runtime."
        >
          backend runner:{" "}
          <button
            onClick={() => setRunnerSettingsOpen(true)}
            className="text-zinc-200 hover:text-emerald-300 underline-offset-2 hover:underline"
            title="Open backend runner settings"
          >
            {cfg?.runner_kind === "echo"
              ? "echo (no LLM)"
              : `${cfg?.runner_kind ?? "—"}${cfg?.runner_model ? ` · ${cfg.runner_model}` : ""}`}
          </button>
          <button
            onClick={() => setExplainerOpen((v) => !v)}
            className="ml-1 w-4 h-4 rounded-full bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-[10px] leading-3 text-zinc-300"
            title="What's this runner?"
            aria-label="Open agent system explainer"
            aria-expanded={explainerOpen}
          >
            ?
          </button>
          {explainerOpen ? (
            <AgentExplainer
              runnerKind={cfg?.runner_kind}
              runnerModel={cfg?.runner_model}
              onClose={() => setExplainerOpen(false)}
            />
          ) : null}
        </span>
        <span title="Browser-side chat LLM — what the 💬 chat panel calls to decompose goals into cards (from 🔌 LLM settings)">
          chat LLM:{" "}
          <span className="text-zinc-200">
            {llmConfig.provider} · {llmConfig.model}
          </span>
        </span>
        <span>tick: {cfg?.polling_interval_ms ?? "—"}ms</span>
      </div>

      <div className="flex items-center gap-2 ml-auto">
        <div className="text-xs text-zinc-400 flex items-center gap-1">
          <span>max_concurrent:</span>
          {editingMax ? (
            <input
              type="number"
              defaultValue={cfg?.max_concurrent_agents}
              autoFocus
              min={1}
              max={50}
              className="bg-zinc-800 border border-zinc-700 rounded px-2 py-0.5 text-xs w-16"
              onBlur={() => setEditingMax(false)}
              onKeyDown={async (e) => {
                if (e.key === "Enter") {
                  const v = parseInt((e.target as HTMLInputElement).value, 10);
                  if (Number.isFinite(v) && v > 0) {
                    try {
                      await api.patchConfig({ max_concurrent_agents: v });
                      void refresh();
                      setNotice({
                        kind: "info",
                        text: `max_concurrent_agents → ${v}`,
                      });
                    } catch (err) {
                      setNotice({ kind: "error", text: (err as Error).message });
                    }
                  }
                  setEditingMax(false);
                } else if (e.key === "Escape") {
                  setEditingMax(false);
                }
              }}
            />
          ) : (
            <button
              onClick={() => setEditingMax(true)}
              className="bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded px-2 py-0.5 text-zinc-100 transition"
            >
              {cfg?.max_concurrent_agents ?? "—"}
            </button>
          )}
        </div>

        <div className="text-xs text-zinc-400 flex items-center gap-3 px-3 border-l border-zinc-800">
          <span>
            active{" "}
            <span className="text-emerald-400 font-medium">
              {totals?.active_workers ?? 0}
            </span>
          </span>
          <span>
            released{" "}
            <span className="text-zinc-200">{totals?.released_today ?? 0}</span>
          </span>
        </div>

        <button
          onClick={async () => {
            const running = totals?.active_workers ?? 0;
            const ok = window.confirm(
              running > 0
                ? `Emergency stop: abort all ${running} running agent(s)? This cannot be undone.`
                : "No agents are currently running. Send emergency stop anyway?",
            );
            if (!ok) return;
            try {
              const r = await api.emergencyStop("operator emergency stop");
              setNotice({
                kind: r.aborted_count > 0 ? "info" : "info",
                text:
                  r.aborted_count > 0
                    ? `Emergency stop: aborted ${r.aborted_count} agent(s)`
                    : "Emergency stop sent — nothing was running",
              });
              void refresh();
            } catch (err) {
              setNotice({ kind: "error", text: (err as Error).message });
            }
          }}
          className="px-3 py-1 text-xs rounded transition bg-rose-600/20 text-rose-300 border border-rose-700 hover:bg-rose-600/30"
          title="Abort all currently running agents"
          aria-label="Emergency stop all running agents"
        >
          ⛔ Stop All
        </button>

        <button
          onClick={() => toggleWf()}
          className={`px-3 py-1 text-xs rounded transition ${
            wfOpen
              ? "bg-amber-500/20 text-amber-300 border border-amber-700"
              : "bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          }`}
          title="Edit WORKFLOW.md"
        >
          {wfOpen ? "← Board" : "📝 Workflow"}
        </button>

        <button
          onClick={() => setChatOpen((v) => !v)}
          className={`px-3 py-1 text-xs rounded transition border ${
            chatOpen
              ? "bg-emerald-500/20 text-emerald-300 border-emerald-700"
              : "bg-zinc-800 hover:bg-zinc-700 border-zinc-700"
          }`}
          title="Decompose a coding goal into kanban cards via the configured LLM"
          aria-label="Toggle chat-to-cards panel"
          aria-pressed={chatOpen}
        >
          💬 Chat
        </button>

        <button
          onClick={() => setRunnerSettingsOpen(true)}
          className="px-3 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title="Configure the backend runner that executes coding tasks (in-memory; restart reverts to WORKFLOW.md)"
          aria-label="Open backend runner settings"
        >
          🔧 Runner
        </button>

        <button
          onClick={() => setLlmSettingsOpen(true)}
          className="px-3 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title="Configure the in-browser chat LLM endpoint (base_url / model / api_key)"
          aria-label="Open LLM endpoint settings"
        >
          🔌 LLM
        </button>

        <button
          onClick={() => setTheme((t) => toggleTheme(t))}
          className="px-2 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title={`Theme: ${theme} — click to switch`}
          aria-label="Toggle light / dark theme"
          aria-pressed={theme === "dark"}
        >
          {theme === "light" ? "☀" : "☾"}
        </button>

        <button
          onClick={() => {
            const k = prompt(
              "Bearer API key for the dashboard (leave empty to clear):",
              "",
            );
            if (k !== null) setApiKey(k);
          }}
          className="px-2 py-1 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          title="Set API key"
        >
          ⚙
        </button>
      </div>

      <LLMSettings
        open={llmSettingsOpen}
        onClose={() => {
          setLlmSettingsOpen(false);
          // Re-read the badge after the modal saves / cancels so the
          // displayed provider+model matches the persisted config.
          setLlmConfig(getStoredLLMConfig());
        }}
      />
      <BackendRunnerSettings
        open={runnerSettingsOpen}
        onClose={() => setRunnerSettingsOpen(false)}
      />
      <ChatPanel open={chatOpen} onClose={() => setChatOpen(false)} />
    </header>
  );
}
