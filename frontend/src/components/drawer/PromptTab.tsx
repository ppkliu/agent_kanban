import Editor from "@monaco-editor/react";
import type { IssueDetail } from "../../api/types";

export default function PromptTab({
  detail,
  onRefresh,
}: {
  detail: IssueDetail;
  onRefresh: () => void;
}) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-900/30 text-xs flex items-center gap-2">
        <span className="text-zinc-400">
          rendered prompt for attempt{" "}
          <span className="text-zinc-200">
            {detail.current_attempt?.attempt_number ?? "—"}
          </span>
        </span>
        <button
          onClick={onRefresh}
          className="ml-auto px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 border border-zinc-700"
        >
          ↻ Re-render with current hints
        </button>
      </div>
      <div className="flex-1 min-h-0">
        <Editor
          height="100%"
          theme="vs-dark"
          language="markdown"
          value={detail.rendered_prompt_preview ?? "(no prompt available)"}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            wordWrap: "on",
            fontSize: 12,
          }}
        />
      </div>
    </div>
  );
}
