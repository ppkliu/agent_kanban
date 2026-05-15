import { useEffect, useRef } from "react";

/** Tiny dictionary of "what is this runner?" copy keyed by the
 * `runner.kind` value emitted by the backend config. Default expected
 * setup is a *local* LLM endpoint (Ollama / vLLM / LM Studio); cloud
 * APIs are documented as alternatives, not the default.
 */
const RUNNER_DESCRIPTIONS: Record<
  string,
  { headline: string; body: string; defaultEndpoint?: string }
> = {
  echo: {
    headline: "EchoRunner — deterministic, no LLM",
    body: "Built-in test runner. Writes a marker file and declares done in one turn. Use this to prove the kanban + orchestrator + Tool API flow without spending any tokens. Switch to a real runner once the pipe works.",
  },
  opencode: {
    headline: "OpenCodeRunner — sst/opencode CLI subprocess",
    body: "Spawns the bundled opencode CLI for each agent attempt. Reads the workspace, edits files, runs commands. Talks to the LLM endpoint configured via OPENCODE_BASE_URL / OPENCODE_PROVIDER / OPENCODE_MODEL.",
    defaultEndpoint:
      "default expected: local LLM endpoint (Ollama / vLLM / LM Studio). Set OPENCODE_BASE_URL to point elsewhere.",
  },
  anthropic_api: {
    headline: "AnthropicAPIRunner — direct /v1/messages",
    body: "Posts to the Anthropic Messages API (or any Anthropic-compatible endpoint — vLLM / LiteLLM proxies all work). Full message control, no CLI wrapping.",
    defaultEndpoint:
      "default expected: local LLM endpoint speaking the Anthropic Messages protocol. Cloud claude.ai works too if you set OPENCODE_API_KEY.",
  },
  claude_cli: {
    headline: "ClaudeCLIRunner — wraps `claude -p`",
    body: "Spawns the Claude Code CLI in headless streaming mode. Requires `claude` on PATH and a valid login.",
    defaultEndpoint:
      "default expected: claude.ai cloud (Anthropic). No local LLM path — the CLI binds to Anthropic's endpoints.",
  },
};

interface Props {
  runnerKind: string | undefined;
  runnerModel: string | undefined;
  onClose: () => void;
}

export default function AgentExplainer({
  runnerKind,
  runnerModel,
  onClose,
}: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // Close on Escape / outside click.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    function onClick(e: MouseEvent) {
      if (
        dialogRef.current &&
        !dialogRef.current.contains(e.target as Node)
      ) {
        onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [onClose]);

  const info = runnerKind
    ? RUNNER_DESCRIPTIONS[runnerKind]
    : undefined;

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-label="Agent system explainer"
      className="absolute top-full left-0 mt-2 z-30 w-80 rounded-md border border-zinc-700 bg-zinc-900 shadow-lg p-4 text-xs text-zinc-200"
    >
      <div className="font-semibold text-sm mb-1">
        What's running my tasks?
      </div>
      <p className="text-zinc-400 mb-3">
        Symphony orchestrates an external <em>agent runner</em>. The runner
        is what actually talks to your LLM and does the coding work.
      </p>

      {info ? (
        <>
          <div className="text-zinc-100 font-medium">
            Current: {info.headline}
          </div>
          {runnerModel ? (
            <div className="text-zinc-400 mb-2">
              model: <span className="text-zinc-200">{runnerModel}</span>
            </div>
          ) : null}
          <p className="text-zinc-300 mb-2">{info.body}</p>
          {info.defaultEndpoint ? (
            <p className="text-zinc-400 italic">{info.defaultEndpoint}</p>
          ) : null}
        </>
      ) : (
        <div className="text-zinc-400">
          Current runner:{" "}
          <span className="text-zinc-200">{runnerKind ?? "(unknown)"}</span>
          . Configure via the `runner.kind` field in WORKFLOW.md.
        </div>
      )}

      <div className="mt-3 pt-2 border-t border-zinc-800 text-zinc-500">
        Full list + swap instructions: see the README's "Four pluggable
        runners" section, or{" "}
        <code className="text-zinc-300">docs/guide/user-manual.md</code>.
      </div>
    </div>
  );
}
