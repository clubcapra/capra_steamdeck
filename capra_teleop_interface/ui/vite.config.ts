import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to ui/dist/ — ui_server.py serves these files at /.
// Dev server proxies /api/* and /state to the Python process at :8765,
// so `npm run dev` gives a hot-reloading UI against a live teleop process.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Single index.html with all assets under /assets/.
    assetsDir: "assets",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/state": "http://127.0.0.1:8765",
      "/estop": "http://127.0.0.1:8765",
      "/resume": "http://127.0.0.1:8765",
      "/strategy": "http://127.0.0.1:8765",
    },
  },
});
