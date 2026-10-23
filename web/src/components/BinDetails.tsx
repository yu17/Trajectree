import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import * as d3 from "d3";
import { api } from "../api/client";
import type { Signal } from "../api/types";
import { SessionSummary } from "./SessionSummary";
import "../timeline/timeline.css";

const fmtDay = d3.utcFormat("%d %b %Y"); // strip buckets are UTC days, weeks or months
const MAX_ITEMS = 5;

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const field = (v: unknown, key: string): unknown =>
  (v !== null && typeof v === "object" ? (v as Record<string, unknown>)[key] : undefined);

// Titles of a `remote` signal's PRs, issues and releases (a release without a title shows its tag).
function remoteTitles(p: Record<string, unknown>): string[] {
  return [...list(p.prs), ...list(p.issues), ...list(p.releases)]
    .map(x => str(field(x, "title")) ?? str(field(x, "tag")))
    .filter((t): t is string => t !== null);
}

// The activity panel for one strip bin: every signal of the project in the bin, by kind.
export function BinDetails({ projectId, start, end }: { projectId: string; start: number; end: number }) {
  const q = useQuery({
    queryKey: ["signals", projectId, start, end],
    // The server matches by overlap, so stop 1 ms before the exclusive end the cell did not count.
    queryFn: () => api.signals([projectId], new Date(start), new Date(end - 1)),
  });
  const signals: Signal[] = q.data ?? [];
  const of = (kind: Signal["kind"]) => signals.filter(s => s.kind === kind);
  const sessions = of("session");
  const commits = of("commits");
  const files = of("files_changed");
  const jobs = of("job_state");
  const remote = of("remote");
  const messages = of("message");
  const first = fmtDay(new Date(start));
  const last = fmtDay(new Date(end - 1));

  return (
    <aside className="details">
      <h2>Activity</h2>
      <p className="muted">{first === last ? first : `${first} – ${last}`}</p>
      {q.isPending ? (
        <p className="muted">loading…</p>
      ) : q.isError ? (
        <p className="empty">Activity unavailable.</p>
      ) : !signals.length ? (
        <p className="empty">No activity in this bin.</p>
      ) : (
        <>
          {sessions.length > 0 && (
            <>
              <h2>Sessions</h2>
              <ul className="links">
                {sessions.map(s => {
                  const sid = str(s.payload.session_id) ?? "";
                  const command = `cd ${str(s.payload.cwd) ?? "."} && claude --resume ${sid}`;
                  return (
                    <li key={s.id}>
                      <b>{str(s.payload.title) ?? sid.slice(0, 8)}</b>
                      {` · ${num(s.payload.prompts)} prompts · ${str(s.payload.model) ?? "unknown model"} `}
                      <CopyResume command={command} />
                      <div><code className="resume">{command}</code></div>
                      {sid && <SessionSummary sessionId={sid} />}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {commits.length > 0 && (
            <>
              <h2>Commits</h2>
              <ul className="links">
                {commits.map(s => {
                  const subjects = list(s.payload.subjects).map(String);
                  return (
                    <li key={s.id}>
                      {num(s.payload.count)} commits
                      {subjects.length > 0 && <ul>{subjects.map((t, i) => <li key={i}>{t}</li>)}</ul>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {files.length > 0 && (
            <>
              <h2>Files</h2>
              <ul className="links">
                {files.map(s => {
                  const paths = list(s.payload.paths).map(String).slice(0, MAX_ITEMS);
                  return (
                    <li key={s.id}>
                      {`${num(s.payload.count)} files changed`}
                      {paths.length > 0 && <ul>{paths.map((p, i) => <li key={i}><code>{p}</code></li>)}</ul>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {jobs.length > 0 && (
            <>
              <h2>Jobs</h2>
              <ul className="links">
                {jobs.map(s => (
                  <li key={s.id}>
                    {`job ${str(s.payload.name) ?? str(s.payload.job_id) ?? "?"} ${str(s.payload.state) ?? "unknown"}`}
                  </li>
                ))}
              </ul>
            </>
          )}
          {remote.length > 0 && (
            <>
              <h2>Remote</h2>
              <ul className="links">
                {remote.map(s => {
                  const titles = remoteTitles(s.payload).slice(0, MAX_ITEMS);
                  return (
                    <li key={s.id}>
                      {`${num(s.payload.count)} updates`}
                      {titles.length > 0 && <ul>{titles.map((t, i) => <li key={i}>{t}</li>)}</ul>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {messages.length > 0 && (
            <>
              <h2>Messages</h2>
              <ul className="links">
                {messages.map(s => (
                  <li key={s.id}>{`${str(s.payload.channel) ?? "message"}: ${str(s.payload.summary) ?? ""}`}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </aside>
  );
}

type CopyState = "idle" | "copied" | "failed";
const COPY_LABEL: Record<CopyState, string> = {
  idle: "Copy resume command", copied: "Copied", failed: "Copy failed, select the text",
};

function CopyResume({ command }: { command: string }) {
  const [state, setState] = useState<CopyState>("idle");
  useEffect(() => {
    if (state === "idle") return;
    const id = setTimeout(() => setState("idle"), state === "copied" ? 1500 : 3000);
    return () => clearTimeout(id);
  }, [state]);
  const copy = () => {
    // navigator.clipboard is absent outside secure contexts (plain http on a LAN host).
    const clip = navigator.clipboard as Clipboard | undefined;
    if (!clip) { setState("failed"); return; }
    clip.writeText(command).then(() => setState("copied"), () => setState("failed"));
  };
  return <button onClick={copy}>{COPY_LABEL[state]}</button>;
}
