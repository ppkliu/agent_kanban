import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Shares the Vite plugin pipeline (React + Tailwind 4) so tests render
// components exactly as production does. Test-only knobs are added here.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./vitest.setup.ts"],
      css: true,
      include: ["src/**/*.test.{ts,tsx}"],
    },
  }),
);
