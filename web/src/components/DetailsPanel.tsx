import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import * as d3 from "d3";
import { api } from "../api/client";
import type { Signal, TimelineResponse, Txn } from "../api/types";
import type { Selection } from "../timeline/Timeline";
import { fmtWhen, glyph } from "../timeline/render";
import "../timeline/timeline.css";

export interface DetailsPanelProps {
  data: TimelineResponse;
  selected: Selection | null;
  onReview: (txnId: string, state: "accepted" | "rejected") => void;
  onRevert: (txnId: string) => void;
}

const fmtRecorded = d3.timeFormat("%d %b %Y %H:%M");

function authorOf(t: Txn): string {
  return [
    `${t.actor_kind} ${t.actor_ref}`,
    t.model,
    t.backend && `via ${t.backend}`,
    t.tool && `tool ${t.tool}`,
  ].filter(Boolean).join(" · ");
}

export function DetailsPanel({ data, selected, onReview, onRevert }: DetailsPanelProps) {
  const event = selected?.type === "event" ? data.events.find(e => e.id === selected.id) ?? null : null;
  // Review the pending txn of the event's latest change (an agent update, say), not its creation.
  const pendingTxn = event ? data.pending.event?.[event.id] : undefined;
  const txnId = pendingTxn ?? event?.created_txn ?? null;
  const isPending = pendingTxn !== undefined;
  const txn = useQuery({
    queryKey: ["txn", txnId],
    queryFn: () => api.txn(txnId!),
    enabled: txnId !== null,
  });

  if (selected?.type === "bin") {
    return <BinDetails projectId={selected.projectId} start={selected.start} end={selected.end} />;
  }

  if (selected?.type === "todo") {
    const todo = data.todos.find(t => t.id === selected.id);
    if (todo) {
      const project = data.projects.find(p => p.id === todo.project_id);
      return (
        <aside className="details">
          <h2>Details</h2>
          <h3>{todo.title}</h3>
          <div><span className="badge">{todo.state}</span><span className="badge">todo</span></div>
          {todo.body_md && <div className="body">{todo.body_md}</div>}
          <dl className="meta">
            <dt>project</dt><dd>{project?.title ?? "inbox"}</dd>
            <dt>due</dt><dd>{todo.due ? fmtRecorded(new Date(todo.due)) : "—"}</dd>
          </dl>
        </aside>
      );
    }
  }

  if (!event) {
    return (
      <aside className="details">
        <h2>Details</h2>
        <p className="empty">Click a block to see its details.</p>
      </aside>
    );
  }

  const project = data.projects.find(p => p.id === event.project_id);
  const author = !txnId ? "—"
    : txn.data ? authorOf(txn.data.txn)
    : txn.isError ? "unavailable"
    : "loading…";
  const wires = data.edges.filter(e => e.src_id === event.id || e.dst_id === event.id);

  return (
    <aside className="details">
      <h2>Details</h2>
      <h3>{glyph[event.kind] ?? "•"} {event.title}</h3>
      <div>
        {isPending && <span className="badge warn">pending</span>}
        <span className="badge">{event.status}</span>
        <span className="badge">{event.kind}</span>
      </div>
      {event.body_md && <div className="body">{event.body_md}</div>}
      <dl className="meta">
        <dt>project</dt><dd>{project?.title ?? "?"}</dd>
        <dt>when</dt><dd>{fmtWhen(new Date(event.t_start), event.t_end ? new Date(event.t_end) : null)}</dd>
        <dt>recorded</dt><dd>{fmtRecorded(new Date(event.created_at))}</dd>
        <dt>author</dt><dd>{author}</dd>
        <dt>txn</dt><dd>{txnId ? <code>{txnId}</code> : "—"}</dd>
      </dl>
      <h2>Wires</h2>
      {wires.length ? (
        <ul className="links">
          {wires.map(w => {
            const out = w.src_id === event.id;
            const otherId = out ? w.dst_id : w.src_id;
            const other = titleOf(data, otherId);
            return (
              <li key={w.id}>
                {out ? "→" : "←"} <b>{w.rel}</b> · {other.title}{" "}
                <span className="muted">({other.project})</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="empty">No wires.</p>
      )}
      {txnId && (
        <div className="actions">
          {isPending ? (
            <>
              <button onClick={() => onReview(txnId, "accepted")}>Accept</button>
              <button onClick={() => onReview(txnId, "rejected")}>Reject</button>
            </>
          ) : (
            <button onClick={() => onRevert(txnId)}>Revert transaction</button>
          )}
        </div>
      )}
    </aside>
  );
}

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number => (typeof v === "number" ? v : 0);

function BinDetails({ projectId, start, end }: { projectId: string; start: number; end: number }) {
  const q = useQuery({
    queryKey: ["signals", projectId, start, end],
    queryFn: () => api.signals([projectId], new Date(start), new Date(end)),
  });
  const signals: Signal[] = q.data ?? [];
  const sessions = signals.filter(s => s.kind === "session");
  const commits = signals.filter(s => s.kind === "commits");

  return (
    <aside className="details">
      <h2>Activity</h2>
      <p className="muted">{fmtRecorded(new Date(start))} → {fmtRecorded(new Date(end))}</p>
      {q.isPending ? (
        <p className="muted">loading…</p>
      ) : q.isError ? (
        <p className="empty">Activity unavailable.</p>
      ) : !sessions.length && !commits.length ? (
        <p className="empty">No activity in this bin.</p>
      ) : (
        <>
          {sessions.length > 0 && (
            <>
              <h2>Sessions</h2>
              <ul className="links">
                {sessions.map(s => {
                  const sid = str(s.payload.session_id) ?? "";
                  return (
                    <li key={s.id}>
                      <b>{str(s.payload.title) ?? sid.slice(0, 8)}</b>
                      {` · ${num(s.payload.prompts)} prompts · ${str(s.payload.model) ?? "unknown model"} `}
                      <CopyResume command={`cd ${str(s.payload.cwd) ?? "."} && claude --resume ${sid}`} />
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
                  const subjects = Array.isArray(s.payload.subjects) ? s.payload.subjects.map(String) : [];
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
        </>
      )}
    </aside>
  );
}

function CopyResume({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(id);
  }, [copied]);
  // navigator.clipboard is absent outside secure contexts (plain http on a LAN host).
  const copy = () => navigator.clipboard?.writeText(command).then(() => setCopied(true), () => {});
  return <button onClick={copy}>{copied ? "Copied" : "Copy resume command"}</button>;
}

function titleOf(data: TimelineResponse, id: string): { title: string; project: string } {
  const item: { title: string; project_id: string | null } | undefined =
    data.events.find(e => e.id === id) ?? data.todos.find(t => t.id === id);
  const project = item ? data.projects.find(p => p.id === item.project_id)?.title : undefined;
  return { title: item?.title ?? id, project: project ?? "?" };
}
