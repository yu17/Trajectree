import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/trajectree/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8742" } },
  test: { environment: "jsdom", setupFiles: ["./vitest.setup.ts"], globals: false },
});
