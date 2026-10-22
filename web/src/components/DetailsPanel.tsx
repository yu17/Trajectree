import { useQuery } from "@tanstack/react-query";
import * as d3 from "d3";
import { api } from "../api/client";
import type { TimelineResponse, Txn } from "../api/types";
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

function titleOf(data: TimelineResponse, id: string): { title: string; project: string } {
  const item: { title: string; project_id: string | null } | undefined =
    data.events.find(e => e.id === id) ?? data.todos.find(t => t.id === id);
  const project = item ? data.projects.find(p => p.id === item.project_id)?.title : undefined;
  return { title: item?.title ?? id, project: project ?? "?" };
}
