import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
    // The SPA talks to the FastAPI app on 8005 during development.
    proxy: {
      "/api": { target: "http://127.0.0.1:8005", changeOrigin: true },
    },
  },
});
