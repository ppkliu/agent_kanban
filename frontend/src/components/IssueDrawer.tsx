import { useEffect, useState } from "react";
import { useStore } from "../store";
import { api } from "../api/client";
import type { IssueDetail } from "../api/types";
import OverviewTab from "./drawer/OverviewTab";
import EventsTab from "./drawer/EventsTab";
import PromptTab from "./drawer/PromptTab";
import HintsTab from "./drawer/HintsTab";
import WorkspaceTab from "./drawer/WorkspaceTab";
import ReplayTab from "./drawer/ReplayTab";

const TABS = [
  "Overview",
  "Events",
  "Prompt",
  "Hints",
  "Workspace",
  "Replay",
] as const;
type Tab = (typeof TABS)[number];

export default function IssueDrawer() {
  const id = useStore((s) => s.selectedIssueId);
  const close = useStore((s) => s.selectIssue);
  const [tab, setTab] = useState<Tab>("Overview");
  const [detail, setDetail] = useState<IssueDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    if (!id) {
      setDetail(null);
      return;
    }
    setLoading(true);
    api
      .issue(id)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [id, version]);

  if (!id) return null;

  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={() => close(null)}
      />
      <aside className="fixed top-0 right-0 h-full w-[70%] max-w-5xl bg-zinc-950 border-l border-zinc-800 z-50 flex flex-col shadow-2xl">
        <header className="flex items-start justify-between gap-4 px-6 py-4 border-b border-zinc-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs text-zinc-400 font-mono">
              <span>{detail?.issue.identifier ?? id}</span>
              {detail?.tracker_url && (
                <a
                  href={detail.tracker_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-blue-400 hover:underline"
                >
                  ↗ tracker
                </a>
              )}
            </div>
            <h2 className="text-lg font-semibold mt-0.5 truncate">
              {detail?.issue.title || (loading ? "Loading…" : id)}
            </h2>
            {detail?.current_attempt && (
              <div className="text-xs text-zinc-400 mt-1">
                State:{" "}
                <span className="text-zinc-200">
                  {detail.current_attempt.state.toUpperCase()}
                </span>{" "}
                · attempt {detail.current_attempt.attempt_number} ·{" "}
                <span className="font-mono">
                  {detail.current_attempt.session_id ?? "no session"}
                </span>
              </div>
            )}
          </div>
          <button
            onClick={() => close(null)}
            className="text-zinc-400 hover:text-zinc-100 text-xl leading-none px-2"
          >
            ×
          </button>
        </header>

        <nav className="flex border-b border-zinc-800 bg-zinc-900/30 px-2">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 text-xs uppercase tracking-wide transition border-b-2 ${
                tab === t
                  ? "border-emerald-500 text-zinc-100"
                  : "border-transparent text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {t}
            </button>
          ))}
        </nav>

        <div className="flex-1 min-h-0 overflow-hidden">
          {!detail ? (
            <div className="p-6 text-zinc-500 text-sm">Loading…</div>
          ) : tab === "Overview" ? (
            <OverviewTab
              detail={detail}
              onMutated={() => setVersion((v) => v + 1)}
            />
          ) : tab === "Events" ? (
            <EventsTab issueId={id} />
          ) : tab === "Prompt" ? (
            <PromptTab detail={detail} onRefresh={() => setVersion((v) => v + 1)} />
          ) : tab === "Hints" ? (
            <HintsTab
              detail={detail}
              onChanged={() => setVersion((v) => v + 1)}
            />
          ) : tab === "Workspace" ? (
            <WorkspaceTab issueId={id} />
          ) : (
            <ReplayTab issueId={id} />
          )}
        </div>
      </aside>
    </>
  );
}
