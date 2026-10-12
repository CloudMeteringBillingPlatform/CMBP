import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the console runs on :5173 and proxies the API (and WebSocket) to the app on :8000.
// In production `npm run build` writes dist/, which the app serves as static files.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/v1": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
