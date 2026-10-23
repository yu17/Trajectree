import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Run } from "../api/types";

export function Proposals({ run, onApplied }: { run: Run; onApplied?: () => void }) {
  const qc = useQueryClient();
  const accept = useMutation({
    mutationFn: (index: number) => api.acceptProposal(run.id, index),
    onSuccess: () => {
      for (const key of ["run", "runs", "timeline", "todos", "txns"]) {
        void qc.invalidateQueries({ queryKey: key === "run" ? ["run", run.id] : [key] });
      }
      onApplied?.();
    },
  });
  return (
    <div>
      <h2>Proposals</h2>
      {run.proposals.length === 0 ? (
        <p className="muted">No proposals</p>
      ) : (
        <ul className="proposals">
          {run.proposals.map((p, i) => (
            <li key={i}>
              <span className="badge">{p.kind}</span> <span className="muted">{p.project}</span> <b>{p.title}</b>{" "}
              <span className="muted">{p.reason}</span>
              {p.when && <span className="muted"> when {p.when}</span>}
              {p.due && <span className="muted"> due {p.due}</span>}{" "}
              {p.applied_txn ? (
                <span className="badge applied">applied</span>
              ) : (
                <button disabled={accept.isPending} onClick={() => accept.mutate(i)}>
                  Accept
                </button>
              )}
              {accept.isError && accept.variables === i && (
                <span className="form-error">{(accept.error as Error).message}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
