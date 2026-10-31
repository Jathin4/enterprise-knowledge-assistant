import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server forwards /api to FastAPI, so the browser sees a single origin (no CORS setup needed).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 127.0.0.1, not "localhost": avoids a ~2 s IPv6-then-IPv4 delay on Windows.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
