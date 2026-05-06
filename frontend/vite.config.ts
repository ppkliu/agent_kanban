import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server proxies REST + WebSocket to the dashboard backend on :7957.
// Production build (npm run build) outputs to ./dist; FastAPI mounts that dir
// via StaticFiles when present, so the same port serves both API and UI.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api/v1/events": {
        target: "ws://127.0.0.1:7957",
        ws: true,
      },
      "/api": {
        target: "http://127.0.0.1:7957",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
