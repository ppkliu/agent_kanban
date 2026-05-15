import { useState } from "react";
import {
  DEFAULT_LLM_CONFIG,
  getStoredLLMConfig,
  resetLLMConfig,
  saveLLMConfig,
  type LLMConfig,
  type LLMProvider,
} from "../llmConfig";
import { testConnection, type TestConnectionResult } from "../llmClient";

interface Props {
  open: boolean;
  onClose: () => void;
}

const PROVIDER_LABELS: Record<LLMProvider, string> = {
  vllm: "vLLM (local, default)",
  ollama: "Ollama (local)",
  "openai-compatible": "OpenAI-compatible",
  anthropic: "Anthropic",
};

const BASE_URL_PLACEHOLDER: Record<LLMProvider, string> = {
  vllm: "http://localhost:8000",
  ollama: "http://localhost:11434",
  "openai-compatible": "http://localhost:8000  (or https://api.openai.com)",
  anthropic: "https://api.anthropic.com",
};

const PROVIDER_NEEDS_KEY: Record<LLMProvider, boolean> = {
  vllm: false,
  ollama: false,
  "openai-compatible": true,
  anthropic: true,
};

export default function LLMSettings({ open, onClose }: Props) {
  const [cfg, setCfg] = useState<LLMConfig>(() => getStoredLLMConfig());
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(
    null,
  );

  if (!open) return null;

  const update = <K extends keyof LLMConfig>(key: K, value: LLMConfig[K]) => {
    setCfg((c) => ({ ...c, [key]: value }));
    // Invalidate any prior test result — fields changed, prior verification
    // no longer reflects what the user would save right now.
    setTestResult(null);
  };

  const handleSave = () => {
    saveLLMConfig(cfg);
    onClose();
  };

  const handleReset = () => {
    const fresh = resetLLMConfig();
    setCfg(fresh);
    setTestResult(null);
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await testConnection(cfg);
      setTestResult(r);
    } finally {
      setTesting(false);
    }
  };

  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
        aria-label="Close LLM settings backdrop"
      />
      <div
        role="dialog"
        aria-label="LLM endpoint settings"
        className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[500px] max-w-[95vw] bg-zinc-900 border border-zinc-700 rounded-md shadow-2xl"
      >
        <header className="flex items-center justify-between px-5 py-3 border-b border-zinc-800">
          <h2 className="text-sm font-semibold">LLM endpoint (browser chat)</h2>
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
            These settings control the LLM that the in-browser chat panel
            calls to decompose your goal into kanban cards. They do{" "}
            <span className="text-zinc-200">not</span> change the backend
            runner (configured in <code>WORKFLOW.md</code> + <code>.env</code>).
            Default is a local Ollama endpoint — no API key needed.
          </p>

          <label className="block">
            <span className="block text-zinc-300 mb-1">Provider</span>
            <select
              value={cfg.provider}
              onChange={(e) => update("provider", e.target.value as LLMProvider)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100"
            >
              {(Object.keys(PROVIDER_LABELS) as LLMProvider[]).map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_LABELS[p]}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="block text-zinc-300 mb-1">Base URL</span>
            <input
              type="text"
              value={cfg.base_url}
              onChange={(e) => update("base_url", e.target.value)}
              placeholder={BASE_URL_PLACEHOLDER[cfg.provider]}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
            />
          </label>

          <label className="block">
            <span className="block text-zinc-300 mb-1">Model</span>
            <input
              type="text"
              value={cfg.model}
              onChange={(e) => update("model", e.target.value)}
              placeholder={DEFAULT_LLM_CONFIG.model}
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
            />
          </label>

          <label className="block">
            <span className="block text-zinc-300 mb-1">
              API key{" "}
              {PROVIDER_NEEDS_KEY[cfg.provider] ? (
                <span className="text-amber-400">(required)</span>
              ) : (
                <span className="text-zinc-500">
                  (not used for vLLM / Ollama)
                </span>
              )}
            </span>
            <input
              type="password"
              value={cfg.api_key}
              onChange={(e) => update("api_key", e.target.value)}
              autoComplete="off"
              className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-zinc-100 font-mono"
            />
          </label>

          {testResult ? (
            <div
              className={`pt-2 border-t text-[11px] ${
                testResult.ok
                  ? "border-emerald-800 text-emerald-300"
                  : "border-rose-800 text-rose-300"
              }`}
              role="status"
              aria-live="polite"
            >
              {testResult.ok ? "✓ Response: " : "× "}
              <code className="text-zinc-100 break-all">
                {testResult.message}
              </code>
            </div>
          ) : null}

          <div className="text-zinc-500 text-[11px] pt-2 border-t border-zinc-800">
            Stored in this browser's <code>localStorage</code>. For local LLM
            endpoints (Ollama / vLLM / LM Studio) this is harmless. For cloud
            providers, anyone with devtools access to this browser can read
            the key. If Ollama refuses the request with a CORS error, start it
            with <code>OLLAMA_ORIGINS=*</code>.
          </div>
        </div>

        <footer className="flex items-center justify-between gap-2 px-5 py-3 border-t border-zinc-800">
          <button
            onClick={handleReset}
            className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
          >
            Reset to defaults
          </button>
          <div className="flex items-center gap-2">
            <button
              onClick={handleTest}
              disabled={testing}
              className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 disabled:opacity-50"
              title="Send a tiny 'reply OK' prompt to the endpoint to verify reachability + auth"
            >
              {testing ? "Testing…" : "Test connection"}
            </button>
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-xs rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              className="px-3 py-1.5 text-xs rounded bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-700"
            >
              Save
            </button>
          </div>
        </footer>
      </div>
    </>
  );
}
