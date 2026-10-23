import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// Polls a run while it is queued or running, then stops.
export function useRun(runId: string | null) {
  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId!),
    enabled: runId !== null,
    refetchInterval: q =>
      q.state.data && ["queued", "running"].includes(q.state.data.run.status) ? 1500 : false,
  });
}
