import { useMemo } from "react";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useStore } from "../store";
import { useProjectStore } from "../projectStore";
import { api } from "../api/client";
import IssueCard from "./IssueCard";
import type { ColumnKey, KanbanEntry } from "../api/types";

const COLUMN_ORDER: { key: ColumnKey; label: string; tint: string }[] = [
  { key: "pending", label: "Pending", tint: "border-zinc-700" },
  { key: "claimed", label: "Claimed", tint: "border-amber-800/40" },
  { key: "running", label: "Running", tint: "border-emerald-800/40" },
  {
    key: "retry_queued",
    label: "Retry-Queued",
    tint: "border-orange-800/40",
  },
  { key: "released", label: "Released", tint: "border-zinc-700/60" },
];

function applyFilters(
  entries: KanbanEntry[],
  filters: ReturnType<typeof useStore.getState>["filters"],
  projectId: string | null,
): KanbanEntry[] {
  const projectLabel = projectId ? `project:${projectId}` : null;
  return entries.filter((e) => {
    const text = filters.text.trim().toLowerCase();
    if (text) {
      const hay = `${e.issue.identifier ?? ""} ${e.issue.title ?? ""}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    if (filters.priority != null && e.issue.priority !== filters.priority)
      return false;
    if (filters.label && !(e.issue.labels ?? []).includes(filters.label))
      return false;
    if (projectLabel !== null) {
      // Phase E2 — narrow to the selected project. Tasks predate this
      // feature when they lack any `project:*` label; treat unlabeled
      // tasks as belonging to `default` so legacy backlogs still show
      // up when "default" is selected.
      const labels = e.issue.labels ?? [];
      const hasProj = labels.some((l) => l.startsWith("project:"));
      if (hasProj) {
        if (!labels.includes(projectLabel)) return false;
      } else {
        if (projectId !== "default") return false;
      }
    }
    return true;
  });
}

function SortableCard({ entry }: { entry: KanbanEntry }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: entry.issue.id });
  const style = {
    transform: CSS.Translate.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
      <IssueCard entry={entry} draggable />
    </div>
  );
}

export default function KanbanBoard() {
  const snapshot = useStore((s) => s.snapshot);
  const filters = useStore((s) => s.filters);
  const setNotice = useStore((s) => s.setNotice);
  const refresh = useStore((s) => s.refresh);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const filtered = useMemo(() => {
    if (!snapshot) return null;
    const out = {} as Record<ColumnKey, KanbanEntry[]>;
    for (const k of COLUMN_ORDER) {
      out[k.key] = applyFilters(
        snapshot.columns[k.key] ?? [],
        filters,
        selectedProjectId,
      );
    }
    return out;
  }, [snapshot, filters, selectedProjectId]);

  if (!snapshot) {
    return (
      <div className="flex items-center justify-center h-full text-zinc-500 text-sm">
        Connecting to dashboard…
      </div>
    );
  }

  async function onDragEnd(event: DragEndEvent) {
    if (!filtered) return;
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const pending = filtered.pending;
    const oldIndex = pending.findIndex((e) => e.issue.id === active.id);
    const newIndex = pending.findIndex((e) => e.issue.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    const reordered = arrayMove(pending, oldIndex, newIndex);
    try {
      await api.reorderPending(reordered.map((e) => e.issue.id));
      void refresh();
      setNotice({ kind: "info", text: "Queue reordered" });
    } catch (e) {
      setNotice({ kind: "error", text: (e as Error).message });
    }
  }

  return (
    <div className="grid grid-cols-5 gap-3 h-full p-4 overflow-hidden">
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={onDragEnd}
      >
        {COLUMN_ORDER.map((c) => {
          const entries = filtered?.[c.key] ?? [];
          const isDraggable = c.key === "pending";
          return (
            <section
              key={c.key}
              className={`flex flex-col bg-zinc-900/40 border ${c.tint} rounded-lg overflow-hidden`}
            >
              <header className="px-3 py-2 border-b border-zinc-800 flex items-center justify-between bg-zinc-900/60">
                <h2 className="text-xs uppercase tracking-wider text-zinc-300 font-semibold">
                  {c.label}
                </h2>
                <span className="text-[10px] text-zinc-500">
                  {entries.length}
                </span>
              </header>
              <div className="flex-1 min-h-0 overflow-auto p-2 space-y-2">
                {isDraggable ? (
                  <SortableContext
                    items={entries.map((e) => e.issue.id)}
                    strategy={verticalListSortingStrategy}
                  >
                    {entries.map((e) => (
                      <SortableCard key={e.issue.id} entry={e} />
                    ))}
                  </SortableContext>
                ) : (
                  entries.map((e) => <IssueCard key={e.issue.id} entry={e} />)
                )}
                {entries.length === 0 && (
                  <div className="text-[11px] text-zinc-600 text-center py-6">
                    (empty)
                  </div>
                )}
              </div>
            </section>
          );
        })}
      </DndContext>
    </div>
  );
}
