import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// The config is also loaded in the worker process, which inherits the env set by the runner; reuse it.
let dataDir = process.env.TRAJECTREE_DATA_DIR;
if (!dataDir) {
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "trajectree-e2e-"));
  process.env.TRAJECTREE_DATA_DIR = dataDir;
  const created = dataDir;
  process.on("exit", () => fs.rmSync(created, { recursive: true, force: true }));
}

export default defineConfig({
  testDir: "./e2e",
  reporter: "list",
  use: { baseURL: "http://127.0.0.1:8799" },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    command: "uv run trajectreed",
    cwd: "..",
    url: "http://127.0.0.1:8799/api/health",
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      TRAJECTREE_DATA_DIR: dataDir,
      TRAJECTREE_PORT: "8799",
      TRAJECTREE_HOST: "127.0.0.1",
    },
  },
});
