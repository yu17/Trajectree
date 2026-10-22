import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Conflict } from "../api/types";
import { errorText } from "../components/errorText";

// Review and revert actions shared by the timeline and the ledger. Both invalidate
// the timeline and the ledger so every view reflects the change.
export function useTxnActions() {
  const qc = useQueryClient();
  const [conflicts, setConflicts] = useState<Conflict[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["timeline"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["txn"] });
  };

  const reviewM = useMutation({
    mutationFn: ({ id, state }: { id: string; state: "accepted" | "rejected" }) => api.review(id, state),
    onSuccess: () => { setError(null); refresh(); },
    onError: err => setError(errorText(err)),
  });

  const revertM = useMutation({
    mutationFn: (id: string) => api.revert(id),
    onSuccess: () => { setConflicts(null); setError(null); refresh(); },
    onError: err => {
      if (err instanceof ApiError && err.status === 409 && Array.isArray(err.body?.conflicts)) {
        setError(null);
        setConflicts(err.body.conflicts as Conflict[]);
      } else {
        setConflicts(null);
        setError(errorText(err));
      }
    },
  });

  return {
    review: (id: string, state: "accepted" | "rejected") => reviewM.mutate({ id, state }),
    revert: (id: string) => {
      if (window.confirm("Revert this transaction?")) revertM.mutate(id);
    },
    conflicts,
    error,
    dismiss: () => { setConflicts(null); setError(null); },
  };
}
