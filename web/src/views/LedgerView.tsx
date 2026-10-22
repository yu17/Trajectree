import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Change, Txn } from "../api/types";
import { errorText } from "../components/errorText";
import { ActionNotice } from "./ActionNotice";
import { useTxnActions } from "./useTxnActions";
import "./views.css";

const PAGE = 50;

const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

function changedKeys(c: Change): string[] {
  const keys = new Set([...Object.keys(c.old ?? {}), ...Object.keys(c.new ?? {})]);
  return [...keys].filter(k => JSON.stringify(c.old?.[k]) !== JSON.stringify(c.new?.[k]));
}

function Diff({ txnId }: { txnId: string }) {
  const q = useQuery({ queryKey: ["txn", txnId], queryFn: () => api.txn(txnId) });
  if (q.isError) return <p className="form-error">{errorText(q.error)}</p>;
  if (!q.data) return <p className="muted">loading…</p>;
  return (
    <div className="diff">
      {q.data.changes.map(c => (
        <div key={c.seq} className="change">
          <div><code>{c.table_name} {c.op} {c.pk.join(", ")}</code></div>
          <table>
            <thead><tr><th>key</th><th>old</th><th>new</th></tr></thead>
            <tbody>
              {changedKeys(c).map(k => (
                <tr key={k}>
                  <th scope="row">{k}</th>
                  <td>{c.old && k in c.old ? show(c.old[k]) : ""}</td>
                  <td>{c.new && k in c.new ? show(c.new[k]) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

function Row({ txn, actions }: { txn: Txn; actions: ReturnType<typeof useTxnActions> }) {
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
        <span className={`badge review-${txn.review}`}>{txn.review}</span>
        <span className="row-actions">
          {txn.review === "pending" && (
            <>
              <button onClick={() => actions.review(txn.id, "accepted")}>Accept</button>
              <button onClick={() => actions.review(txn.id, "rejected")}>Reject</button>
            </>
          )}
          {!txn.reverted_by && <button onClick={() => actions.revert(txn.id)}>Revert</button>}
        </span>
      </div>
      {open && <Diff txnId={txn.id} />}
    </li>
  );
}

export function LedgerView() {
  const [before, setBefore] = useState<string | undefined>(undefined);
  const actions = useTxnActions();
  const q = useQuery({ queryKey: ["txns", before], queryFn: () => api.txns(PAGE, before) });
  const rows = q.data ?? [];

  return (
    <section className="ledger">
      <ActionNotice conflicts={actions.conflicts} error={actions.error} onDismiss={actions.dismiss} />
      {q.isError && <p className="form-error">{errorText(q.error)}</p>}
      {q.isPending && <p className="muted">loading…</p>}
      <ul className="ledger-list">
        {rows.map(t => <Row key={t.id} txn={t} actions={actions} />)}
      </ul>
      {rows.length === PAGE && (
        <button onClick={() => setBefore(rows[rows.length - 1].id)}>Load more</button>
      )}
      {before && <button onClick={() => setBefore(undefined)}>Back to newest</button>}
    </section>
  );
}
