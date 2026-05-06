import { useEffect } from "react";
import { useStore } from "../store";

export default function Notice() {
  const notice = useStore((s) => s.notice);
  const clear = useStore((s) => s.clearNotice);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(clear, notice.kind === "error" ? 8000 : 3500);
    return () => clearTimeout(t);
  }, [notice, clear]);

  if (!notice) return null;

  return (
    <div
      className={`fixed bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 rounded shadow-lg text-sm z-50 border ${
        notice.kind === "error"
          ? "bg-rose-900/90 text-rose-100 border-rose-700"
          : "bg-zinc-800/90 text-zinc-100 border-zinc-700"
      }`}
    >
      {notice.text}
      <button
        onClick={clear}
        className="ml-3 text-xs opacity-70 hover:opacity-100"
      >
        ×
      </button>
    </div>
  );
}
