import { useEffect } from "react";
import { useStore } from "./store";
import { loadStoredApiKey } from "./api/client";
import { applyTheme, getStoredTheme } from "./theme";
import TopBar from "./components/TopBar";
import FilterBar from "./components/FilterBar";
import KanbanBoard from "./components/KanbanBoard";
import IssueDrawer from "./components/IssueDrawer";
import ActivityFeed from "./components/ActivityFeed";
import WorkflowEditor from "./components/WorkflowEditor";
import Notice from "./components/Notice";

export default function App() {
  const init = useStore((s) => s.init);
  const workflowEditorOpen = useStore((s) => s.workflowEditorOpen);

  useEffect(() => {
    applyTheme(getStoredTheme());
    loadStoredApiKey();
    init();
  }, [init]);

  return (
    <div className="flex flex-col h-screen bg-zinc-950 text-zinc-100">
      <TopBar />
      <FilterBar />
      <main className="flex-1 min-h-0 overflow-hidden">
        {workflowEditorOpen ? <WorkflowEditor /> : <KanbanBoard />}
      </main>
      <IssueDrawer />
      <ActivityFeed />
      <Notice />
    </div>
  );
}
