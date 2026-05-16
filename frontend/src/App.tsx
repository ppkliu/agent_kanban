import { useEffect } from "react";
import { useStore } from "./store";
import { useProjectStore } from "./projectStore";
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
  const refreshProjects = useProjectStore((s) => s.refresh);

  useEffect(() => {
    applyTheme(getStoredTheme());
    loadStoredApiKey();
    init();
    // One-shot fetch of the project list on mount. Subsequent refreshes
    // are driven by the projectStore's own create / rename / archive
    // mutators rather than a periodic poll.
    void refreshProjects();
  }, [init, refreshProjects]);

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
