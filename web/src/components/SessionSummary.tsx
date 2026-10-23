import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { errorText } from "./errorText";
import { Proposals } from "./Proposals";
import { useRun } from "./useRun";

const ACTIVE = ["queued", "running"];

// A session's `Summarize session` button and, once started, the run it queued.
export function SessionSummary({ sessionId }: { sessionId: string }) {
  const qc = useQueryClient();
  const start = useMutation({
    mutationFn: () => api.summarizeSession(sessionId, {}),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["runs"] }),
  });
  const detail = useRun(start.data?.id ?? null);
  const run = detail.data?.run ?? start.data;
  const active = run !== undefined && ACTIVE.includes(run.status);
  return (
    <div className="session-summary">
      <button disabled={start.isPending || active} onClick={() => start.mutate()}>Summarize session</button>
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
