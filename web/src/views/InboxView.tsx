import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Conflict, InspectionDue, ModelChoice, Todo, Txn } from "../api/types";
import { errorText } from "../components/errorText";
import { ModelPicker } from "../components/ModelPicker";
import { useRun } from "../components/useRun";
import { Proposals } from "../components/Proposals";
import { ActionNotice } from "./ActionNotice";
import { Diff } from "./LedgerView";
import { useTxnActions } from "./useTxnActions";
import "./views.css";

// Shared with App, which shows the count on the Inbox tab.
export const pendingTxnsQuery = {
  queryKey: ["txns", "pending"],
  queryFn: () => api.txns(200, undefined, { review: "pending" }),
};

// Shared with App, which counts the projects that can be started on the Inbox tab.
export const inspectionsQuery = {
  queryKey: ["inspections"],
  queryFn: () => api.inspections(),
};

// A project is due when it has stretches to inspect and no inspection running.
export const isDue = (d: InspectionDue) => d.active_run === null && d.stretches > 0;

const refreshInspections = (qc: ReturnType<typeof useQueryClient>) => {
  void qc.invalidateQueries({ queryKey: ["inspections"] });
  void qc.invalidateQueries({ queryKey: ["runs"] });
  void qc.invalidateQueries({ queryKey: ["timeline"] });
};

function DueRow({ item }: { item: InspectionDue }) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState<ModelChoice | null>(null);
  const start = useMutation({
    mutationFn: (c: ModelChoice) => api.inspect(item.project_id, c),
    onSettled: () => refreshInspections(qc),
  });
  const run = useRun(item.active_run);
  const active = item.active_run !== null;
  return (
    <li className="ledger-row">
      <div className="ledger-head">
        <span>{`${item.title} · ${item.stretches} stretches · ${item.first_day} → ${item.last_day} · ${item.sessions} sessions, ${item.commits} commits, ${item.file_days} file days`}</span>
        {active && item.episode !== null && item.episodes !== null && (
          <span className="muted">{`episode ${item.episode} of ${item.episodes}`}</span>
        )}
        {run.data && <span className={`badge run-${run.data.run.status}`}>{run.data.run.status}</span>}
        <span className="row-actions">
          <ModelPicker value={choice} onChange={setChoice} />
          <button disabled={choice === null || active || start.isPending}
            onClick={() => choice && start.mutate(choice)}>
            Start
          </button>
        </span>
      </div>
      {start.isError && <p className="form-error">{errorText(start.error)}</p>}
    </li>
  );
}

function DueSection({ items }: { items: InspectionDue[] }) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState<ModelChoice | null>(null);
  const startAll = useMutation({
    mutationFn: (c: ModelChoice) => api.inspectAll(c),
    onSettled: () => refreshInspections(qc),
  });
  const skipped = startAll.data?.skipped ?? [];
  return (
    <>
      <h2>{`Due for inspection (${items.length})`}</h2>
      <div className="row-actions">
        <ModelPicker value={choice} onChange={setChoice} label="Model for all" />
        <button disabled={choice === null || items.length === 0 || startAll.isPending}
          onClick={() => choice && startAll.mutate(choice)}>
          Start all
        </button>
      </div>
      {startAll.isError && <p className="form-error">{errorText(startAll.error)}</p>}
      {skipped.length > 0 && <p className="muted">{`Skipped: ${skipped.join(", ")}`}</p>}
      {items.length === 0 && <p className="muted">Nothing to inspect</p>}
      <ul className="inbox-list">
        {items.map(d => <DueRow key={d.project_id} item={d} />)}
      </ul>
    </>
  );
}

// Due ascending; undated todos last.
function byDue(a: Todo, b: Todo): number {
  if (a.due === null || b.due === null) return (a.due === null ? 1 : 0) - (b.due === null ? 1 : 0);
  return Date.parse(a.due) - Date.parse(b.due);
}

function PendingRow({ txn, actions, onReject }: {
  txn: Txn; actions: ReturnType<typeof useTxnActions>; onReject?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="ledger-row">
      <div className="ledger-head">
        <time dateTime={txn.ts}>{new Date(txn.ts).toLocaleString()}</time>
        <span className="actor">{txn.actor_kind}/{txn.actor_ref}</span>
        {txn.model && <span className="model">{txn.model}</span>}
        <button className="linklike intent" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          {txn.intent}
        </button>
        <span className="row-actions">
          <button onClick={() => actions.review(txn.id, "accepted")}>Accept</button>
          <button onClick={() => { onReject?.(); actions.review(txn.id, "rejected"); }}>Reject</button>
        </span>
      </div>
      {open && <Diff txnId={txn.id} />}
    </li>
  );
}

function PendingGroup({ runId, txns, actions }: {
  runId: string | null; txns: Txn[]; actions: ReturnType<typeof useTxnActions>;
}) {
  const qc = useQueryClient();
  const run = useRun(runId);
  const [conflicted, setConflicted] = useState(false);
  const [failure, setFailure] = useState<{ failed: string; conflicts: Conflict[] } | null>(null);
  const ids = txns.map(t => t.id);
  const many = useMutation({
    mutationFn: (state: "accepted" | "rejected") => api.reviewMany(ids, state),
    onSuccess: r => setFailure(r.failed === null ? null : { failed: r.failed, conflicts: r.conflicts }),
    // Refresh after a partial result too: the finished ones left the pending list.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["timeline"] });
      void qc.invalidateQueries({ queryKey: ["txns"] });
      void qc.invalidateQueries({ queryKey: ["txn"] });
      void qc.invalidateQueries({ queryKey: ["todos"] });
    },
  });
  const r = run.data?.run;
  const heading = runId === null
    ? "Other writes"
    : r ? `${r.kind} · ${r.model ?? r.runner} · ${new Date(r.created_at).toLocaleString()}` : runId;
  return (
    <div className="pending-group">
      <h3>{heading}</h3>
      <div className="row-actions">
        {/* Named apart from the section-wide Accept all above the groups. */}
        <button aria-label="Accept all in this group" disabled={many.isPending}
          onClick={() => many.mutate("accepted")}>Accept all</button>
        <button disabled={many.isPending} onClick={() => many.mutate("rejected")}>Reject all</button>
        {conflicted && actions.conflicts && (
          <button disabled={many.isPending} onClick={() => { actions.dismiss(); setConflicted(false); many.mutate("rejected"); }}>
            Reject the whole group
          </button>
        )}
      </div>
      {many.isError && <p className="form-error">{errorText(many.error)}</p>}
      {failure && (
        <p className="form-error">
          {`Stopped at ${failure.failed}`}
          {failure.conflicts.length > 0 && `: ${failure.conflicts.map(c => `${c.table_name} ${c.op} ${c.pk.join(", ")}`).join("; ")}`}
        </p>
      )}
      <ul className="inbox-list">
        {txns.map(t => <PendingRow key={t.id} txn={t} actions={actions} onReject={() => setConflicted(true)} />)}
      </ul>
    </div>
  );
}

// Writes of one run sit together, runs in order of first appearance; writes without a run come last.
function groupByRun(rows: Txn[]): [string | null, Txn[]][] {
  const groups = new Map<string | null, Txn[]>();
  for (const t of rows) groups.set(t.run_id, [...(groups.get(t.run_id) ?? []), t]);
  const out = [...groups];
  return [...out.filter(([k]) => k !== null), ...out.filter(([k]) => k === null)];
}

export function InboxView() {
  const qc = useQueryClient();
  const actions = useTxnActions();
  const pending = useQuery(pendingTxnsQuery);
  const inspections = useQuery(inspectionsQuery);
  const todos = useQuery({ queryKey: ["todos", "open"], queryFn: () => api.todos(["open", "doing"]) });
  // Project titles are needed only for todos filed under a project.
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.projects(),
    enabled: (todos.data ?? []).some(t => t.project_id !== null),
  });

  const acceptAll = useMutation({
    mutationFn: async (ids: string[]) => {
      for (const id of ids) await api.review(id, "accepted");
    },
    // Refresh after a partial failure too: the accepted ones left the pending list.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["timeline"] });
      void qc.invalidateQueries({ queryKey: ["txns"] });
      void qc.invalidateQueries({ queryKey: ["txn"] });
    },
  });

  const proposals = useQuery({
    queryKey: ["runs", "open-proposals"],
    queryFn: () => api.runs({ openProposals: true, limit: 50 }),
  });

  const rows = pending.data ?? [];
  const dueItems = (inspections.data ?? []).filter(d => d.stretches > 0 || d.active_run !== null);
  const open = [...(todos.data ?? [])].sort(byDue);
  const projectOf = (t: Todo) =>
    t.project_id === null ? "inbox" : projects.data?.find(p => p.id === t.project_id)?.title ?? "?";

  return (
    <section className="inbox">
      <ActionNotice
        conflicts={actions.conflicts}
        error={actions.error ?? (acceptAll.error ? errorText(acceptAll.error) : null)}
        onDismiss={() => { actions.dismiss(); acceptAll.reset(); }}
      />
      <DueSection items={dueItems} />
      {inspections.isError && <p className="form-error">{errorText(inspections.error)}</p>}
      <h2>{`Pending writes (${rows.length})`}</h2>
      <button disabled={rows.length === 0 || acceptAll.isPending}
        onClick={() => acceptAll.mutate(rows.map(t => t.id))}>
        Accept all
      </button>
      {pending.isError && <p className="form-error">{errorText(pending.error)}</p>}
      {pending.isPending && <p className="muted">loading…</p>}
      {pending.isSuccess && rows.length === 0 && <p>Nothing to review</p>}
      {groupByRun(rows).map(([runId, txns]) => (
        <PendingGroup key={runId ?? "none"} runId={runId} txns={txns} actions={actions} />
      ))}
      <h2>Proposals</h2>
      {proposals.isError && <p className="form-error">{errorText(proposals.error)}</p>}
      {proposals.isSuccess && proposals.data.length === 0 && <p className="muted">No proposals</p>}
      {(proposals.data ?? []).map(run => (
        <div key={run.id}>
          <p className="muted">{`${run.kind} · ${new Date(run.created_at).toLocaleString()} · ${run.result_md}`}</p>
          <Proposals run={run} />
        </div>
      ))}
      <h2>Open todos</h2>
      {todos.isError && <p className="form-error">{errorText(todos.error)}</p>}
      {todos.isSuccess && open.length === 0 && <p className="muted">No open todos</p>}
      <ul className="inbox-list">
        {open.map(t => (
          <li key={t.id}>
            {`${t.title} · ${projectOf(t)} · ${t.due ? new Date(t.due).toLocaleDateString() : "no due"}`}
          </li>
        ))}
      </ul>
    </section>
  );
}
