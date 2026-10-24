import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelChoice } from "../api/types";
import { errorText } from "./errorText";
import { ModelPicker } from "./ModelPicker";
import { Proposals } from "./Proposals";
import { useRun } from "./useRun";

const ACTIVE = ["queued", "running"];

// A session's `Summarize session` button and, once started, the run it queued.
export function SessionSummary({ sessionId }: { sessionId: string }) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState<ModelChoice | null>(null);
  const start = useMutation({
    mutationFn: (c: ModelChoice) => api.summarizeSession(sessionId, c),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["runs"] }),
  });
  const detail = useRun(start.data?.id ?? null);
  const run = detail.data?.run ?? start.data;
  const active = run !== undefined && ACTIVE.includes(run.status);
  return (
    <div className="session-summary">
      <ModelPicker value={choice} onChange={setChoice} />
      <button disabled={choice === null || start.isPending || active} onClick={() => choice && start.mutate(choice)}>Summarize session</button>
      {start.isError && <p className="form-error">{errorText(start.error)}</p>}
      {detail.isError && <p className="form-error">{errorText(detail.error)}</p>}
      {run && (
        <div>
          <span className={`badge run-${run.status}`}>{run.status}</span>
          {run.result_md && <pre className="answer">{run.result_md}</pre>}
          {run.error && <p className="form-error">{run.error}</p>}
          {!active && detail.data && <Proposals run={detail.data.run} />}
        </div>
      )}
    </div>
  );
}
