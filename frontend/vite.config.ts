import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };   // Node global; avoids adding @types/node

// The dev server forwards /api to FastAPI, so the browser sees a single origin (no CORS setup needed).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 127.0.0.1, not "localhost": avoids a ~2 s IPv6-then-IPv4 delay on Windows.
    // Set API_PORT if 8000 is taken by another app.
    proxy: { "/api": `http://127.0.0.1:${process.env.API_PORT ?? 8000}` },
  },
});
