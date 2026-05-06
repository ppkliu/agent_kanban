import { useEffect, useState } from "react";
import Editor from "@monaco-editor/react";
import { useStore } from "../store";
import { api } from "../api/client";
import type { ConfigDTO } from "../api/types";

export default function WorkflowEditor() {
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);
  const [content, setContent] = useState("");
  const [parsed, setParsed] = useState<ConfigDTO | null>(null);
  const [path, setPath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .workflow()
      .then((r) => {
        setContent(r.content);
        setParsed(r.config);
        setPath(r.path);
      })
      .catch((e: Error) =>
        setNotice({ kind: "error", text: `workflow load: ${e.message}` }),
      );
  }, [setNotice]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const result = await api.putWorkflow(content);
      if (result.ok) {
        setParsed(result.config);
        setNotice({ kind: "info", text: "WORKFLOW.md saved & reloaded" });
        void refresh();
      } else {
        setError(result.error);
        setNotice({
          kind: "error",
          text: `parse failed (kept previous): ${result.error}`,
        });
      }
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="h-full grid grid-cols-[1fr_400px] divide-x divide-zinc-800">
      <div className="flex flex-col">
        <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-900/30 text-xs flex items-center gap-3">
          <span className="font-mono text-zinc-400">{path}</span>
          <button
            onClick={save}
            disabled={saving}
            className="ml-auto px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white"
          >
            {saving ? "Saving…" : "Save & Reload"}
          </button>
        </div>
        <div className="flex-1 min-h-0">
          <Editor
            height="100%"
            theme="vs-dark"
            language="markdown"
            value={content}
            onChange={(v) => setContent(v ?? "")}
            options={{
              minimap: { enabled: false },
              fontSize: 12,
              wordWrap: "on",
            }}
          />
        </div>
      </div>
      <aside className="overflow-auto p-4 space-y-4 text-xs">
        {error && (
          <div className="p-3 bg-rose-900/30 border border-rose-700 rounded">
            <div className="font-semibold text-rose-200 mb-1">parse error</div>
            <pre className="whitespace-pre-wrap text-rose-100">{error}</pre>
            <p className="mt-2 text-rose-300/80">
              The file on disk and the live config were rolled back to the
              previous version.
            </p>
          </div>
        )}
        <div>
          <h3 className="uppercase tracking-wider text-zinc-400 mb-2">
            Parsed front matter
          </h3>
          {parsed ? (
            <table className="w-full">
              <tbody>
                {Object.entries(parsed).map(([k, v]) => (
                  <tr key={k} className="border-b border-zinc-800/40">
                    <td className="py-1 pr-2 text-zinc-500 font-mono whitespace-nowrap">
                      {k}
                    </td>
                    <td className="py-1 text-zinc-200 break-all">
                      {Array.isArray(v) ? v.join(", ") : String(v)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="text-zinc-500 italic">loading…</div>
          )}
        </div>
        <div className="p-3 bg-zinc-900/40 border border-zinc-800 rounded text-zinc-400 text-[11px] leading-relaxed">
          ⚠ Saving with malformed YAML returns 422 — the previous config
          stays live in memory and on disk. WORKFLOW.md is the source of truth
          for tracker, runner, hooks, and the prompt body.
        </div>
      </aside>
    </div>
  );
}
