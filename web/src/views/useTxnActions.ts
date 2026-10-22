import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Conflict } from "../api/types";
import { errorText } from "../components/errorText";

// Review and revert actions shared by the timeline, the ledger and the inbox. They
// invalidate the timeline and every txns query (the inbox's pending list included) so
// every view reflects the change.
export function useTxnActions() {
  const qc = useQueryClient();
  const [conflicts, setConflicts] = useState<Conflict[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["timeline"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["txn"] });
    // A reject reverts the write, which may remove a todo the inbox lists.
    void qc.invalidateQueries({ queryKey: ["todos"] });
  };

  // A reject reverts the transaction, so it can hit the same 409 conflict as a revert.
  const onError = (err: Error) => {
    if (err instanceof ApiError && err.status === 409 && Array.isArray(err.body?.conflicts)) {
      setError(null);
      setConflicts(err.body.conflicts as Conflict[]);
    } else {
      setConflicts(null);
      setError(errorText(err));
    }
  };

  const reviewM = useMutation({
    mutationFn: ({ id, state }: { id: string; state: "accepted" | "rejected" }) => api.review(id, state),
    onSuccess: () => { setConflicts(null); setError(null); refresh(); },
    onError,
  });

  const revertM = useMutation({
    mutationFn: (id: string) => api.revert(id),
    onSuccess: () => { setConflicts(null); setError(null); refresh(); },
    onError,
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
