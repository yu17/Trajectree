import type { RunnerStatus } from "../api/types";

export function statusLine(s: RunnerStatus): string {
  if (!s.claude.available) return "claude: not installed";
  if (!s.claude.authenticated) return "claude: not logged in (run trajectree login)";
  return `claude ${s.claude.version ?? ""} · logged in`.replace("  ", " ");
}
