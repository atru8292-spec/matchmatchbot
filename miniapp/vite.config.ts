import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// base: "/app/" — мини-апп будет отдаваться nginx с этого пути (тот же origin, что и бот).
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    host: "127.0.0.1",
    // Прокси API на локальный бэкенд (uvicorn :8000) — тот же origin для фронта,
    // без CORS. В проде мини-апп отдаётся nginx с того же origin, что и /api.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
