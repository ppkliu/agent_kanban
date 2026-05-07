import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// React Testing Library cleans up DOM between tests.
afterEach(() => {
  cleanup();
});

// jsdom doesn't ship matchMedia / ResizeObserver — Tailwind's CSS uses them.
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// Stub out Monaco editor — it's huge and not exercised by component tests.
vi.mock("@monaco-editor/react", async () => {
  const React = await import("react");
  return {
    default: (props: {
      value?: string;
      onChange?: (v: string | undefined) => void;
      options?: { readOnly?: boolean };
    }) =>
      React.createElement("textarea", {
        "data-testid": "monaco-stub",
        value: props.value ?? "",
        readOnly: props.options?.readOnly,
        onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) =>
          props.onChange?.(e.target.value),
      }),
  };
});

// Stub xterm + addon-fit (also heavy, not exercised by current tests).
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    open() {}
    write() {}
    dispose() {}
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class {} }));
