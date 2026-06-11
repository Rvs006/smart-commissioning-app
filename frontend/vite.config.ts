import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        app: resolve(__dirname, "index.html"),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        changeOrigin: true,
        target: "http://127.0.0.1:8000"
      }
    }
  }
});
