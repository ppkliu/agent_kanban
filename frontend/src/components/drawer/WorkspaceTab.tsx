import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { FilePreview, WorkspaceListing } from "../../api/types";

function fmtSize(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function WorkspaceTab({ issueId }: { issueId: string }) {
  const [listing, setListing] = useState<WorkspaceListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  useEffect(() => {
    setError(null);
    api
      .workspace(issueId)
      .then(setListing)
      .catch((e: Error) => setError(e.message));
  }, [issueId]);

  useEffect(() => {
    if (!selected) {
      setPreview(null);
      return;
    }
    setLoadingPreview(true);
    api
      .workspaceFile(issueId, selected)
      .then(setPreview)
      .catch(() => setPreview(null))
      .finally(() => setLoadingPreview(false));
  }, [selected, issueId]);

  if (error) {
    return (
      <div className="p-6 text-rose-300 text-sm">workspace error: {error}</div>
    );
  }
  if (!listing) {
    return <div className="p-6 text-zinc-500 text-sm">Loading…</div>;
  }
  if (!listing.exists) {
    return (
      <div className="p-6 text-zinc-500 text-sm">
        Workspace directory{" "}
        <code className="bg-zinc-900 px-1 rounded">{listing.workspace_path}</code>{" "}
        does not exist yet (issue not dispatched).
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 h-full">
      <div className="border-r border-zinc-800 overflow-auto">
        <div className="px-4 py-2 text-[11px] text-zinc-500 font-mono border-b border-zinc-800">
          {listing.workspace_path}
        </div>
        <ul className="text-xs">
          {listing.entries.map((e) => (
            <li key={e.path}>
              {e.is_dir ? (
                <div className="px-4 py-1 text-zinc-500">📁 {e.path}/</div>
              ) : (
                <button
                  className={`w-full text-left px-4 py-1 hover:bg-zinc-800 flex justify-between ${
                    selected === e.path ? "bg-zinc-800 text-zinc-100" : "text-zinc-300"
                  }`}
                  onClick={() => setSelected(e.path)}
                >
                  <span className="font-mono truncate">📄 {e.path}</span>
                  <span className="text-zinc-600 ml-2 shrink-0">
                    {fmtSize(e.size)}
                  </span>
                </button>
              )}
            </li>
          ))}
          {listing.entries.length === 0 && (
            <li className="px-4 py-3 text-zinc-500 italic">empty</li>
          )}
        </ul>
      </div>
      <div className="overflow-auto bg-zinc-950">
        {loadingPreview ? (
          <div className="p-6 text-zinc-500 text-sm">Loading…</div>
        ) : preview ? (
          <div className="text-xs">
            <div className="px-4 py-2 border-b border-zinc-800 text-zinc-400 font-mono flex items-center justify-between">
              <span>{preview.path}</span>
              <span className="text-zinc-600">
                {fmtSize(preview.size)}
                {preview.truncated ? " · truncated" : ""}
              </span>
            </div>
            <pre className="p-4 leading-relaxed whitespace-pre overflow-x-auto">
              {preview.lines.map((l, i) => (
                <div key={i} className="flex">
                  <span className="text-zinc-700 select-none w-10 text-right pr-3">
                    {i + 1}
                  </span>
                  <span className="text-zinc-200 whitespace-pre-wrap break-all">
                    {l}
                  </span>
                </div>
              ))}
            </pre>
          </div>
        ) : (
          <div className="p-6 text-zinc-500 text-sm">
            select a file to preview (read-only, capped at 200 lines)
          </div>
        )}
      </div>
    </div>
  );
}
