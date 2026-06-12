import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Kept separate from vite.config.ts so the dev-server proxy and build inputs
// stay untouched; tests never need the /api proxy because fetch is mocked.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // The async render/poll tests (App shell, ModulePage discovery wiring) take
    // 8-11s on a cold install where Vitest's bundler compiles for the first
    // time; CI runners are always cold. The 5s default is too tight there.
    testTimeout: 20000,
  },
});
