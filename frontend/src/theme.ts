/** Theme module — persists between sessions via localStorage. Default is
 * `light` (warm-stone palette); operators can toggle to `dark` via the
 * TopBar button. Theme is applied as a `data-theme="…"` attribute on the
 * <html> element so CSS overrides in index.css can flip palette tokens
 * without re-rendering React. */

export type Theme = "light" | "dark";

const STORAGE_KEY = "symphony.theme";

export function getStoredTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const v = window.localStorage.getItem(STORAGE_KEY);
  return v === "dark" ? "dark" : "light";
}

export function applyTheme(theme: Theme): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = theme;
  }
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, theme);
  }
}

export function toggleTheme(current: Theme): Theme {
  const next: Theme = current === "light" ? "dark" : "light";
  applyTheme(next);
  return next;
}
