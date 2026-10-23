import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Todo, Txn } from "../api/types";
import { errorText } from "../components/errorText";
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

// Due ascending; undated todos last.
function byDue(a: Todo, b: Todo): number {
  if (a.due === null || b.due === null) return (a.due === null ? 1 : 0) - (b.due === null ? 1 : 0);
  return Date.parse(a.due) - Date.parse(b.due);
}

function PendingRow({ txn, actions }: { txn: Txn; actions: ReturnType<typeof useTxnActions> }) {
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
          <button onClick={() => actions.review(txn.id, "rejected")}>Reject</button>
        </span>
      </div>
      {open && <Diff txnId={txn.id} />}
    </li>
  );
}

export function InboxView() {
  const qc = useQueryClient();
  const actions = useTxnActions();
  const pending = useQuery(pendingTxnsQuery);
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
      <h2>{`Pending writes (${rows.length})`}</h2>
      <button disabled={rows.length === 0 || acceptAll.isPending}
        onClick={() => acceptAll.mutate(rows.map(t => t.id))}>
        Accept all
      </button>
      {pending.isError && <p className="form-error">{errorText(pending.error)}</p>}
      {pending.isPending && <p className="muted">loading…</p>}
      {pending.isSuccess && rows.length === 0 && <p>Nothing to review</p>}
      <ul className="inbox-list">
        {rows.map(t => <PendingRow key={t.id} txn={t} actions={actions} />)}
      </ul>
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
