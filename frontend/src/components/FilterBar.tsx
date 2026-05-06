import { useMemo } from "react";
import { useStore } from "../store";

export default function FilterBar() {
  const filters = useStore((s) => s.filters);
  const setFilter = useStore((s) => s.setFilter);
  const snapshot = useStore((s) => s.snapshot);

  const labels = useMemo(() => {
    if (!snapshot) return [] as string[];
    const set = new Set<string>();
    for (const col of Object.values(snapshot.columns)) {
      for (const e of col) {
        for (const l of e.issue.labels ?? []) set.add(l);
      }
    }
    return [...set].sort();
  }, [snapshot]);

  return (
    <div className="flex items-center gap-3 px-6 py-2 bg-zinc-900/20 border-b border-zinc-800 text-xs">
      <input
        placeholder="Search title or identifier…"
        className="flex-1 max-w-xl bg-zinc-900 border border-zinc-800 rounded px-3 py-1.5 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-600"
        value={filters.text}
        onChange={(e) => setFilter({ text: e.target.value })}
      />

      <select
        className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1.5"
        value={filters.priority ?? ""}
        onChange={(e) =>
          setFilter({
            priority: e.target.value ? Number(e.target.value) : null,
          })
        }
      >
        <option value="">all priorities</option>
        {[1, 2, 3, 4, 5].map((p) => (
          <option key={p} value={p}>
            p:{p}
          </option>
        ))}
      </select>

      <select
        className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1.5 min-w-32"
        value={filters.label ?? ""}
        onChange={(e) =>
          setFilter({ label: e.target.value || null })
        }
      >
        <option value="">all labels</option>
        {labels.map((l) => (
          <option key={l} value={l}>
            {l}
          </option>
        ))}
      </select>

      {(filters.text || filters.priority != null || filters.label) && (
        <button
          className="text-zinc-400 hover:text-zinc-100"
          onClick={() =>
            setFilter({ text: "", priority: null, label: null, agent: null })
          }
        >
          clear filters
        </button>
      )}
    </div>
  );
}
