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
      TRAJECTREE_INGEST_EVERY: "0",
      TRAJECTREE_CLAUDE_HOME: "",
      TRAJECTREE_BACKENDS_ROOT: "",
      TRAJECTREE_CODEX_SESSIONS: "", // never the developer's real Codex rollouts
      TRAJECTREE_CLAUDE_BIN: path.resolve(import.meta.dirname,"../tests/bin/fake-claude"),
      // Runs are confined: fake-claude and the repo's interpreter live outside the system dirs.
      TRAJECTREE_CONFINE_EXTRA_RO: [
        path.resolve(import.meta.dirname, "../tests/bin"),
        path.resolve(import.meta.dirname, "../.venv"),
      ].join(":"),
      TRAJECTREE_CFG_DIR: path.join(dataDir, "cfg"),
      TRAJECTREE_RUN_ENV_PASSTHROUGH: "FAKE_CLAUDE_MODE,FAKE_CLAUDE_STRUCTURED,FAKE_CLAUDE_RESULT",
      FAKE_CLAUDE_MODE: "ok",
      FAKE_CLAUDE_RESULT: "Review done",
      FAKE_CLAUDE_STRUCTURED: JSON.stringify({
        answer: "Parsed one decision",
        proposals: [{ kind: "event", project: "clmi", title: "Encoder frozen (parsed)", reason: "quick add", event_kind: "decision" }],
      }),
    },
  },
});
