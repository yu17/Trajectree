import type { RunnerInfo, RunnerStatus } from "../api/types";

function cliLine(name: string, info: RunnerInfo, login: string): string {
  if (!info.available) return `${name}: not installed`;
  const head = info.version ? `${name} ${info.version}` : name;
  return info.authenticated ? `${head} · logged in` : `${head} · not logged in (${login})`;
}

// One line per runner: the two CLIs, then each ccx backend profile.
export function statusLines(s: RunnerStatus): string[] {
  return [
    cliLine("claude", s.claude, "run trajectree login"),
    cliLine("codex", s.codex, "run codex login --device-auth"),
    ...s.backends.map(b => `ccx:${b.name} · ${b.credentials ? "credentials set" : "no credentials in its profile"}`),
  ];
}

export function RunnerStatusList({ status }: { status: RunnerStatus }) {
  return (
    <ul className="runner-status muted" aria-label="Runners">
      {statusLines(status).map(line => <li key={line}>{line}</li>)}
    </ul>
  );
}
