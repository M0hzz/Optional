/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development `npm run dev` serves on 127.0.0.1:5173 and forwards /api to the API on 8600.
// `npm run build` writes dist/, which the optlab API serves at /. Route views are split with React.lazy
// (App.tsx), so the first page loads only the dashboard; charts load with the views that use them.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8600" },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 600 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
